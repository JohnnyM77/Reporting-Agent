"""Every agent's send function goes through shared/email_service.py and keeps
its own failure policy. SMTP is mocked; the assertions are on what would
have gone over the wire."""

from __future__ import annotations

import email
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from shared import email_service as es  # noqa: E402

ENV = {"EMAIL_FROM": "bot@example.com", "EMAIL_TO": "me@example.com", "EMAIL_APP_PASSWORD": "pw"}


@pytest.fixture
def smtp(monkeypatch):
    for k in ("EMAIL_USER", "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASS"):
        monkeypatch.delenv(k, raising=False)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    with mock.patch.object(es.smtplib, "SMTP_SSL") as m:
        yield m


def _sent(smtp_mock):
    server = smtp_mock.return_value.__enter__.return_value
    server.login.assert_called_once_with("bot@example.com", "pw")
    msg = server.send_message.call_args.args[0]
    return email.message_from_bytes(msg.as_bytes())


def _types(parsed):
    return [p.get_content_type() for p in parsed.walk()]


@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "BHP_results.pdf"
    p.write_bytes(b"%PDF-1.4 fake")
    return p


def test_bob_sends_html_with_pdf_and_brother_address(smtp, pdf, tmp_path):
    import agent

    agent.send_email("Bob digest", "text", "<p>html</p>", attachments=[pdf, tmp_path / "missing.pdf"])
    parsed = _sent(smtp)
    assert parsed["Subject"] == "Bob digest" and parsed["To"] == "me@example.com"
    assert _types(parsed) == ["multipart/mixed", "multipart/alternative", "text/plain", "text/html", "application/pdf"]

    smtp.reset_mock()
    agent.send_email("Bro", "text", to_addr="bro@example.com")
    assert _sent(smtp)["To"] == "bro@example.com"


def test_bob_raises_when_email_env_missing(monkeypatch, smtp):
    import agent

    monkeypatch.delenv("EMAIL_APP_PASSWORD")
    with pytest.raises(es.EmailConfigError):
        agent.send_email("s", "b")
    smtp.assert_not_called()


def test_bob_raises_on_smtp_failure(smtp):
    import agent

    smtp.side_effect = OSError("smtp down")
    with pytest.raises(OSError):
        agent.send_email("s", "b")


@pytest.mark.parametrize("module", ["sgx_agent", "us_agent"])
def test_slinger_and_usa_attach_pdfs_under_cap(module, smtp, pdf, tmp_path, monkeypatch):
    mod = __import__(module)
    big = tmp_path / "big.pdf"
    big.write_bytes(b"x" * 200)
    monkeypatch.setattr(mod, "MAX_ATTACHMENT_TOTAL_BYTES", 100)
    assert mod._send_email("digest", "t", "<p>h</p>", attachments=[pdf, big]) is True
    parsed = _sent(smtp)
    names = [p.get_filename() for p in parsed.walk() if p.get_filename()]
    assert names == ["BHP_results.pdf"]


@pytest.mark.parametrize("module", ["sgx_agent", "us_agent"])
def test_slinger_and_usa_never_raise(module, smtp, monkeypatch):
    mod = __import__(module)
    smtp.side_effect = OSError("down")
    assert mod._send_email("s", "t", "<p>h</p>") is False
    monkeypatch.delenv("EMAIL_TO")
    assert mod._send_email("s", "t", "<p>h</p>") is False


def test_ned_raises_on_failure_and_sends_html(smtp):
    from ned import main as ned_main

    ned_main.send_email("Ned", "plain", "<p>h</p>")
    assert _types(_sent(smtp)) == ["multipart/alternative", "text/plain", "text/html"]
    smtp.side_effect = OSError("down")
    with pytest.raises(OSError):
        ned_main.send_email("Ned", "plain", "<p>h</p>")


def test_wally_inline_chart_and_workbook(smtp, tmp_path):
    from wally.config import load_email_settings
    from wally.email_report import send_email

    png = tmp_path / "chart.png"
    png.write_bytes(b"\x89PNG")
    xlsx = tmp_path / "BHP.xlsx"
    xlsx.write_bytes(b"PK")
    ok = send_email(load_email_settings(), "Wally", "t", '<img src="cid:c1">', [xlsx, png],
                    inline_images=[("c1", png)])
    assert ok
    parsed = _sent(smtp)
    types = _types(parsed)
    assert types[:5] == ["multipart/mixed", "multipart/related", "multipart/alternative", "text/plain", "text/html"]
    assert types.count("image/png") == 1  # the inline chart is not attached twice
    assert [p.get_filename() for p in parsed.walk() if p.get("Content-Disposition", "").startswith("attachment")] == ["BHP.xlsx"]


def test_wally_missing_settings_raise_at_load(monkeypatch):
    from wally.config import load_email_settings

    for k in ("EMAIL_FROM", "EMAIL_USER", "EMAIL_TO"):
        monkeypatch.delenv(k, raising=False)
    with pytest.raises(RuntimeError, match="Missing email settings"):
        load_email_settings()


def test_generic_sender_used_by_harry_and_theo(smtp, pdf):
    from email_sender import send_summary_email

    assert send_summary_email("Harry", "body", [pdf], body_html="<p>h</p>") is True
    assert "application/pdf" in _types(_sent(smtp))
    smtp.side_effect = OSError("down")
    assert send_summary_email("Harry", "body") is False


def test_harry_runner_sends_through_generic_sender(smtp, pdf):
    from hindsight import runner

    assert runner._send_email("Harry", "body", "<p>h</p>", [pdf]) is True
    assert _sent(smtp)["Subject"] == "Harry"


def test_master_engine_notifier(smtp):
    from master_engine.notifier import send_email

    assert send_email("Master", "plain", "<p>h</p>") is True
    assert _sent(smtp)["Subject"] == "Master"
