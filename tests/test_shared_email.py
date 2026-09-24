"""shared/email_service.py: the one SMTP transport every agent sends through."""

from __future__ import annotations

import email
from pathlib import Path
from unittest import mock

import pytest

from shared import email_service as es

GMAIL_ENV = {"EMAIL_FROM": "bob@example.com", "EMAIL_TO": "me@example.com", "EMAIL_APP_PASSWORD": "pw"}


def _settings(**kw) -> es.SmtpSettings:
    return es.SmtpSettings.from_env(env={**GMAIL_ENV, **kw})


# --- config ------------------------------------------------------------------

def test_from_env_gmail_defaults_match_bob_ned_slinger():
    s = _settings()
    assert (s.email_from, s.email_to, s.smtp_user, s.smtp_password) == (
        "bob@example.com", "me@example.com", "bob@example.com", "pw")
    assert (s.smtp_host, s.smtp_port) == ("smtp.gmail.com", 465)
    assert s.missing() == []


def test_from_env_smtp_overrides_match_wally_sally():
    s = es.SmtpSettings.from_env(env={
        "EMAIL_USER": "w@example.com", "EMAIL_TO": "me@example.com",
        "SMTP_HOST": "smtp.x", "SMTP_PORT": "587", "SMTP_USER": "u", "SMTP_PASS": "p",
        "EMAIL_APP_PASSWORD": "ignored"})
    assert (s.email_from, s.smtp_user, s.smtp_password, s.smtp_host, s.smtp_port) == (
        "w@example.com", "u", "p", "smtp.x", 587)


def test_empty_secret_falls_back_to_default():
    """An unset GitHub secret arrives as "" -- that must not become host ""."""
    s = _settings(SMTP_HOST="", SMTP_PORT="", SMTP_USER="", SMTP_PASS="")
    assert (s.smtp_host, s.smtp_port, s.smtp_user, s.smtp_password) == (
        "smtp.gmail.com", 465, "bob@example.com", "pw")


def test_to_addr_overrides_email_to_and_values_are_stripped():
    s = es.SmtpSettings.from_env(env={**GMAIL_ENV, "EMAIL_APP_PASSWORD": " pw\n"}, to_addr="bro@example.com")
    assert s.email_to == "bro@example.com" and s.smtp_password == "pw"


def test_missing_lists_every_gap():
    assert es.SmtpSettings.from_env(env={}).missing() == [
        "EMAIL_FROM / EMAIL_USER", "EMAIL_TO", "SMTP_USER", "SMTP_PASS / EMAIL_APP_PASSWORD"]


# --- MIME --------------------------------------------------------------------

def test_plain_html_and_pdf_attachment(tmp_path):
    pdf = tmp_path / "report.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    atts = es.load_attachments([pdf])
    msg = es.build_message(email_from="a@x", email_to="b@x", subject="S", body_text="plain £",
                           body_html="<p>html</p>", attachments=atts)
    parsed = email.message_from_bytes(msg.as_bytes())
    types = [p.get_content_type() for p in parsed.walk()]
    assert types == ["multipart/mixed", "multipart/alternative", "text/plain", "text/html", "application/pdf"]
    assert parsed["Subject"] == "S"
    pdf_part = [p for p in parsed.walk() if p.get_content_type() == "application/pdf"][0]
    assert pdf_part.get_filename() == "report.pdf" and pdf_part.get_payload(decode=True) == b"%PDF-1.4 fake"


def test_inline_images_use_related_structure(tmp_path):
    png = tmp_path / "chart.png"
    png.write_bytes(b"\x89PNG fake")
    xlsx = tmp_path / "book.xlsx"
    xlsx.write_bytes(b"PK")
    msg = es.build_message(email_from="a@x", email_to="b@x", subject="S", body_text="t",
                           body_html='<img src="cid:c1">', attachments=es.load_attachments([xlsx]),
                           inline_images=[("c1", png)])
    parsed = email.message_from_bytes(msg.as_bytes())
    types = [p.get_content_type() for p in parsed.walk()]
    assert types[:5] == ["multipart/mixed", "multipart/related", "multipart/alternative", "text/plain", "text/html"]
    img = [p for p in parsed.walk() if p.get_content_type() == "image/png"][0]
    assert img["Content-ID"] == "<c1>"
    assert types[-1] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def test_attachment_display_name_tuple_and_explicit_type(tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"data")
    atts = es.load_attachments([(f, "Nice Name.xlsx"), es.Attachment.from_path(f, maintype="application", subtype="pdf")])
    assert atts[0].filename == "Nice Name.xlsx" and atts[0].subtype.endswith("sheet")
    assert (atts[1].maintype, atts[1].subtype) == ("application", "pdf")


def test_cap_and_unreadable_are_skipped_not_fatal(tmp_path):
    small, big = tmp_path / "a.pdf", tmp_path / "b.pdf"
    small.write_bytes(b"x" * 10)
    big.write_bytes(b"x" * 100)
    logs: list[str] = []
    atts = es.load_attachments([small, tmp_path / "missing.pdf", big], max_total_bytes=50, log=logs.append)
    assert [a.filename for a in atts] == ["a.pdf"]
    assert any("could not read" in l for l in logs) and any("cap reached" in l for l in logs)


# --- send --------------------------------------------------------------------

def test_send_email_uses_smtp_ssl_login_and_send():
    with mock.patch.object(es.smtplib, "SMTP_SSL") as smtp:
        ok = es.send_email("Subj", "body", "<p>b</p>", settings=_settings(), log=lambda m: None)
    assert ok
    smtp.assert_called_once()
    assert smtp.call_args.args[:2] == ("smtp.gmail.com", 465)
    server = smtp.return_value.__enter__.return_value
    server.login.assert_called_once_with("bob@example.com", "pw")
    sent = server.send_message.call_args.args[0]
    assert sent["To"] == "me@example.com" and sent["Subject"] == "Subj"


def test_missing_config_returns_false_or_raises():
    bad = es.SmtpSettings.from_env(env={})
    with mock.patch.object(es.smtplib, "SMTP_SSL") as smtp:
        assert es.send_email("s", "b", settings=bad, log=lambda m: None) is False
        with pytest.raises(es.EmailConfigError):
            es.send_email("s", "b", settings=bad, raise_on_error=True)
    smtp.assert_not_called()


def test_smtp_failure_returns_false_or_raises():
    with mock.patch.object(es.smtplib, "SMTP_SSL", side_effect=OSError("down")):
        assert es.send_email("s", "b", settings=_settings(), log=lambda m: None) is False
        with pytest.raises(OSError):
            es.send_email("s", "b", settings=_settings(), raise_on_error=True, log=lambda m: None)


def test_attachment_names_and_recipient_stay_out_of_logs_by_default(tmp_path):
    """Actions logs are public; Harry's PDF filenames name tickers."""
    pdf = tmp_path / "NHC_sell_review.pdf"
    pdf.write_bytes(b"%PDF")
    logs: list[str] = []
    with mock.patch.object(es.smtplib, "SMTP_SSL"):
        es.send_email("s", "b", attachments=[pdf], settings=_settings(), log=logs.append)
    assert not any("NHC" in l or "me@example.com" in l for l in logs), logs
    logs.clear()
    with mock.patch.object(es.smtplib, "SMTP_SSL"):
        es.send_email("s", "b", attachments=[pdf], settings=_settings(), log=logs.append, log_details=True)
    assert any("NHC_sell_review.pdf" in l for l in logs)
