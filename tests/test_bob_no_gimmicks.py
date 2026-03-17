# tests/test_bob_no_gimmicks.py
#
# Tests verifying that Bob's joke/cartoon/silence gimmick code has been removed
# and that the no-announcements path is plain and non-blocking.
#
# Covers:
#   A. No announcements → plain no-announcements text, no crash, no network calls
#   B. Real announcements present → processed normally, no gimmick functions
#   C. NHC rerun case → NHC results processed, no gimmick code path, no crash

import sys
import types
from pathlib import Path

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies so agent.py can be imported in CI
# ---------------------------------------------------------------------------
for _stub in (
    "anthropic", "playwright", "playwright.async_api", "googleapiclient",
    "googleapiclient.discovery", "googleapiclient.http",
    "google", "google.oauth2", "google.oauth2.credentials",
    "google.oauth2.service_account", "google.auth",
    "google.auth.transport", "google.auth.transport.requests",
):
    if _stub not in sys.modules:
        sys.modules[_stub] = types.ModuleType(_stub)

_pw_stub = types.ModuleType("playwright_fetch")
_pw_stub.fetch_pdf_with_playwright = None  # type: ignore[attr-defined]
sys.modules.setdefault("playwright_fetch", _pw_stub)

sys.path.insert(0, str(Path(__file__).parent.parent))

import agent  # noqa: E402


# ---------------------------------------------------------------------------
# A. No announcements
# ---------------------------------------------------------------------------

class TestNoAnnouncements:
    def test_build_email_no_announcements_returns_plain_message(self):
        """build_email with empty lists must include a plain no-announcements line."""
        body_text, body_html = agent.build_email([], [], [])
        assert "No reportable announcements found in the last" in body_text
        assert "No reportable announcements found in the last" in body_html

    def test_build_email_no_announcements_no_joke_or_cartoon(self):
        """No-announcements output must not contain joke or cartoon text."""
        body_text, body_html = agent.build_email([], [], [])
        for banned in ("joke", "cartoon", "cagle", "ladder", "political cartoon"):
            assert banned.lower() not in body_text.lower(), (
                f"banned term '{banned}' found in plain-text output"
            )
            assert banned.lower() not in body_html.lower(), (
                f"banned term '{banned}' found in HTML output"
            )

    def test_build_email_no_announcements_no_network_required(self):
        """build_email must not raise even when no network is available."""
        # No mock/patch needed — the function should never attempt network calls.
        body_text, body_html = agent.build_email([], [], [])
        assert body_text  # non-empty
        assert body_html  # non-empty

    def test_build_email_no_silence_section_header(self):
        """HTML output must not contain a 'SILENCE' section header."""
        _, body_html = agent.build_email([], [], [])
        assert "SILENCE" not in body_html


# ---------------------------------------------------------------------------
# B. Real announcements present
# ---------------------------------------------------------------------------

class TestRealAnnouncements:
    def test_build_email_with_high_impact_no_gimmick_text(self):
        """High-impact announcements are rendered; no joke/cartoon text appears."""
        blocks = ["NHC: Half-year results beat expectations.\nOpen: https://asx.com.au/nhc\n"]
        body_text, body_html = agent.build_email(blocks, [], [])
        assert "HIGH IMPACT" in body_text
        assert "NHC" in body_text
        for banned in ("joke", "cartoon", "cagle"):
            assert banned.lower() not in body_text.lower()
            assert banned.lower() not in body_html.lower()

    def test_build_email_with_material_no_gimmick_text(self):
        """Material announcements are rendered; no joke/cartoon text appears."""
        blocks = ["BHP: Quarterly update.\nOpen: https://asx.com.au/bhp\n"]
        body_text, body_html = agent.build_email([], blocks, [])
        assert "MATERIAL" in body_text
        assert "BHP" in body_text
        for banned in ("joke", "cartoon", "cagle"):
            assert banned.lower() not in body_text.lower()
            assert banned.lower() not in body_html.lower()

    def test_build_email_with_fyi_only_no_gimmick_text(self):
        """FYI-only announcements are rendered; no joke/cartoon text appears."""
        blocks = ["CBA: Board change.\nOpen: https://asx.com.au/cba\n"]
        body_text, body_html = agent.build_email([], [], blocks)
        assert "FYI" in body_text
        assert "CBA" in body_text
        for banned in ("joke", "cartoon", "cagle"):
            assert banned.lower() not in body_text.lower()
            assert banned.lower() not in body_html.lower()

    def test_no_gimmick_functions_exported(self):
        """Gimmick helper functions must not exist on the agent module."""
        assert not hasattr(agent, "fetch_joke_of_the_day"), (
            "fetch_joke_of_the_day should have been removed"
        )
        assert not hasattr(agent, "fetch_cartoon_of_the_day"), (
            "fetch_cartoon_of_the_day should have been removed"
        )
        assert not hasattr(agent, "build_silence_line"), (
            "build_silence_line should have been removed"
        )

    def test_no_gimmick_urls_in_module(self):
        """Gimmick API URLs must not be present as module-level constants."""
        assert not hasattr(agent, "JOKE_API_URL"), (
            "JOKE_API_URL should have been removed"
        )
        assert not hasattr(agent, "CARTOON_PAGE_URL"), (
            "CARTOON_PAGE_URL should have been removed"
        )
        assert not hasattr(agent, "FUN_CONTENT_TIMEOUT_SECS"), (
            "FUN_CONTENT_TIMEOUT_SECS should have been removed"
        )


# ---------------------------------------------------------------------------
# C. NHC rerun case
# ---------------------------------------------------------------------------

class TestNHCRerun:
    def test_nhc_blocks_processed_without_gimmick(self):
        """NHC results block can be emitted without any gimmick code path."""
        nhc_block = (
            "NHC: H1 FY25 net profit $42m, up 18% on pcp. "
            "Strong coal price and operational performance.\n"
            "Open: https://www.asx.com.au/announcements/NHC/nhc-h1-fy25.pdf\n"
        )
        body_text, body_html = agent.build_email([nhc_block], [], [])
        assert "NHC" in body_text
        assert "HIGH IMPACT" in body_text
        for banned in ("joke", "cartoon", "cagle", "silence"):
            assert banned.lower() not in body_text.lower()
            assert banned.lower() not in body_html.lower()

    def test_no_announcements_message_includes_hours_back(self):
        """Plain no-announcements message includes the configured look-back window."""
        body_text, _ = agent.build_email([], [], [])
        assert str(agent.HOURS_BACK) in body_text

    def test_build_email_signature_no_silence_line_param(self):
        """build_email must accept exactly (high_impact, material, fyi) — no silence_line."""
        import inspect
        sig = inspect.signature(agent.build_email)
        params = list(sig.parameters.keys())
        assert "silence_line" not in params, (
            "silence_line parameter should have been removed from build_email"
        )
        assert params == ["high_impact", "material", "fyi"]
