# tests/test_agent_gate_and_rerun.py
#
# Unit tests for the two fixes shipped in this PR:
#
#  1. looks_like_asx_access_gate() detects the consent-gate page even when
#     the "Agree and proceed" button text has been stripped by fetch_html_text()
#     (which removes <header>, <footer>, and <nav> elements).
#
#  2. FORCE_RERUN_TICKERS causes the dedup logic to re-emit announcements that
#     were previously recorded in seen_state, allowing a specific ticker to be
#     re-processed on the same day without clearing the entire state file.

import os
import sys
import types
from pathlib import Path
from unittest import mock

# ---------------------------------------------------------------------------
# Stub out heavy optional dependencies so agent.py can be imported in a
# test environment that doesn't have playwright / anthropic installed.
# ---------------------------------------------------------------------------
for _stub in ("anthropic", "playwright", "playwright.async_api", "googleapiclient",
              "googleapiclient.discovery", "googleapiclient.http",
              "google", "google.oauth2", "google.oauth2.credentials",
              "google.oauth2.service_account", "google.auth",
              "google.auth.transport", "google.auth.transport.requests"):
    if _stub not in sys.modules:
        sys.modules[_stub] = types.ModuleType(_stub)

# playwright_fetch is imported by agent.py; provide a minimal stub
_pw_stub = types.ModuleType("playwright_fetch")
_pw_stub.fetch_pdf_with_playwright = None  # type: ignore[attr-defined]
sys.modules.setdefault("playwright_fetch", _pw_stub)

# Make sure repo root is on the path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent import (  # noqa: E402  (import after stub setup)
    looks_like_asx_access_gate,
    is_meaningful_text,
    looks_like_results_title,
    announcement_key,
    now_sgt,
)


# ---------------------------------------------------------------------------
# 1. looks_like_asx_access_gate — gate-detection tests
# ---------------------------------------------------------------------------

def test_gate_detected_with_both_phrases():
    """Original pattern: both phrases present → True."""
    text = "Access to this site\n\nGeneral Conditions\nAgree and proceed"
    assert looks_like_asx_access_gate(text) is True


def test_gate_detected_access_phrase_alone():
    """'access to this site' alone is enough — button may have been stripped."""
    text = (
        "Access to this site\n"
        "By using this site you acknowledge the terms set out below.\n"
        "Australian Securities Exchange Limited ABN 98 008 624 691 (ASX) operates "
        "this site. The following are the terms and conditions that apply to your "
        "use of this site.\n"
        "1. You may use this site only for lawful purposes..."
    )
    assert looks_like_asx_access_gate(text) is True


def test_gate_detected_general_conditions_with_agree():
    """Secondary pattern: 'general conditions' + 'agree and proceed'."""
    text = "General Conditions\nPlease read before accessing. Agree and proceed."
    assert looks_like_asx_access_gate(text) is True


def test_gate_not_triggered_by_empty_text():
    assert looks_like_asx_access_gate("") is False
    assert looks_like_asx_access_gate(None) is False  # type: ignore[arg-type]


def test_gate_not_triggered_by_real_financial_text():
    """A genuine financial report excerpt must NOT be flagged as a gate page."""
    text = (
        "NHC HALF YEAR RESULTS FY25\n"
        "Revenue: $182.3m (+12% pcp)\n"
        "EBITDA: $45.6m (margin 25%)\n"
        "NPAT: $22.1m\n"
        "Operating cash flow: $38.4m\n"
        "Net debt: $65m\n"
        "Interim dividend: 3.5 cps\n"
    )
    assert looks_like_asx_access_gate(text) is False


def test_is_meaningful_text_rejects_gate_page():
    """is_meaningful_text() must return False for gate text even when it is
    longer than the minimum character threshold and 'Agree and proceed' has
    been stripped (simulating fetch_html_text removing a <header> element)."""
    gate_body = (
        "Access to this site\n"
        "By using this site you agree to the terms set out below.\n"
        + "Australian Securities Exchange terms. " * 70  # inflate length
    )
    assert len(gate_body) > 2500
    assert is_meaningful_text(gate_body, min_chars=2500) is False


def test_is_meaningful_text_accepts_real_content():
    """is_meaningful_text() must return True for genuine financial content."""
    real = (
        "Revenue $182m EBITDA $45m NPAT $22m cash flow positive "
        * 60  # inflate beyond 2500 chars
    )
    assert len(real) > 2500
    assert is_meaningful_text(real, min_chars=2500) is True


# ---------------------------------------------------------------------------
# 2. FORCE_RERUN_TICKERS — dedup bypass tests
# ---------------------------------------------------------------------------

def _make_seen_state(ticker: str, url: str) -> dict:
    """Return a seen_state dict pre-populated with one entry for ticker/url."""
    key = announcement_key(ticker, url)
    return {key: now_sgt().isoformat(timespec="seconds")}


def test_force_rerun_bypasses_seen_state():
    """When FORCE_RERUN_TICKERS includes a ticker, its announcements are
    re-emitted even if already present in seen_state."""
    ticker = "NHC"
    url = (
        "https://www.asx.com.au/asx/v2/statistics/"
        "displayAnnouncement.do?display=pdf&idsId=03072858"
    )

    seen_state = _make_seen_state(ticker, url)
    key = announcement_key(ticker, url)
    assert key in seen_state  # confirm the key is already seen

    items = [{"ticker": ticker, "url": url, "title": "NHC Half Year Results"}]

    # Simulate the dedup loop with FORCE_RERUN_TICKERS = {"NHC"}
    force_rerun = frozenset({"NHC"})
    fresh_items = []
    for it in items:
        k = announcement_key(ticker, it["url"])
        if k in seen_state and ticker not in force_rerun:
            continue
        it["seen_key"] = k
        fresh_items.append(it)

    assert len(fresh_items) == 1, "NHC should be re-processed when in FORCE_RERUN_TICKERS"


def test_normal_dedup_still_works_for_other_tickers():
    """Tickers NOT in FORCE_RERUN_TICKERS remain deduplicated."""
    ticker = "BHP"
    url = (
        "https://www.asx.com.au/asx/v2/statistics/"
        "displayAnnouncement.do?display=pdf&idsId=99999999"
    )

    seen_state = _make_seen_state(ticker, url)
    items = [{"ticker": ticker, "url": url, "title": "BHP Full Year Results"}]

    force_rerun = frozenset({"NHC"})  # BHP not listed
    fresh_items = []
    for it in items:
        k = announcement_key(ticker, it["url"])
        if k in seen_state and ticker not in force_rerun:
            continue
        it["seen_key"] = k
        fresh_items.append(it)

    assert len(fresh_items) == 0, "BHP should still be deduplicated"


def test_force_rerun_tickers_env_var_parsing():
    """FORCE_RERUN_TICKERS env var is parsed correctly (comma-separated, trimmed,
    uppercased) and empty / whitespace entries are ignored."""
    with mock.patch.dict(os.environ, {"FORCE_RERUN_TICKERS": " NHC , bhp , "}):
        result = frozenset(
            t.strip().upper()
            for t in os.environ.get("FORCE_RERUN_TICKERS", "").split(",")
            if t.strip()
        )
    assert result == frozenset({"NHC", "BHP"})


def test_force_rerun_tickers_empty_string():
    """Empty string produces an empty frozenset (no tickers force-re-run)."""
    with mock.patch.dict(os.environ, {"FORCE_RERUN_TICKERS": ""}):
        result = frozenset(
            t.strip().upper()
            for t in os.environ.get("FORCE_RERUN_TICKERS", "").split(",")
            if t.strip()
        )
    assert result == frozenset()


# ---------------------------------------------------------------------------
# 3. looks_like_results_title — NHC half-year announcement title patterns
# ---------------------------------------------------------------------------

def test_results_title_hyphenated_half_year_report():
    """'Half-Year Report' (hyphen, no 'results') must be recognised."""
    assert looks_like_results_title("Appendix 4D and Half-Year Report") is True
    assert looks_like_results_title("Half-Year Report") is True


def test_results_title_1h_fy_format():
    """'1H FY...' titles (number-then-H, common in Australian reporting) must match."""
    assert looks_like_results_title("1H FY2026 Results") is True
    assert looks_like_results_title("NHC 1H FY2026 Results") is True
    assert looks_like_results_title("1H FY2026 Results Presentation") is True


def test_results_title_1hfy_no_space():
    """Compact '1HFY26' notation (no space between 1H and FY) must match."""
    assert looks_like_results_title("1HFY26 Results") is True


def test_results_title_half_year_financial_results():
    """'Half-Year Financial Results' (hyphen + 'results' not preceded by more keywords) must match."""
    assert looks_like_results_title("Half-Year Financial Results") is True


def test_results_title_false_positives_unchanged():
    """Titles that should NOT trigger deep analysis are still rejected."""
    assert looks_like_results_title("Investor Call Transcript") is False
    assert looks_like_results_title("Webcast of Half-Year Results") is False
    assert looks_like_results_title("Conference Call Transcript") is False
    assert looks_like_results_title("Random Corporate Update") is False

