# tests/test_bob_state_machine.py
#
# Tests for the announcement state machine added in the "Fix Bob state handling
# to prevent missed earnings" PR.
#
# Covers:
#   1. is_high_priority_title() — keyword detection for earnings/results
#   2. should_process_item()    — full decision matrix
#   3. mark_state()             — state writing + retry_count increment
#   4. load_seen_state()        — backward-compat with old {key: timestamp} format
#   5. prune_seen_state()       — new Dict[str, Dict] format
#   6. Mandatory NHC scenario   — first run FAILS, second run retries and COMPLETES

import sys
import types
import datetime as dt
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

from agent import (  # noqa: E402
    STATUS_NEW,
    STATUS_PROCESSING,
    STATUS_COMPLETED,
    STATUS_FAILED,
    MAX_RETRIES,
    HOURS_BACK,
    HIGH_PRIORITY_KEYWORDS,
    announcement_key,
    is_high_priority_title,
    should_process_item,
    mark_state,
    load_seen_state,
    prune_seen_state,
    now_sgt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed_entry(ticker: str, url: str, headline: str = "Test Headline") -> dict:
    key = announcement_key(ticker, url)
    return {
        key: {
            "announcement_id": key,
            "ticker": ticker,
            "headline": headline,
            "status": STATUS_COMPLETED,
            "retry_count": 0,
            "last_attempt": now_sgt().isoformat(timespec="seconds"),
            "error": "",
        }
    }


def _make_failed_entry(
    ticker: str,
    url: str,
    retry_count: int = 1,
    hours_ago: float = 1.0,
    headline: str = "Test Headline",
) -> dict:
    key = announcement_key(ticker, url)
    last_attempt = (now_sgt() - dt.timedelta(hours=hours_ago)).isoformat(timespec="seconds")
    return {
        key: {
            "announcement_id": key,
            "ticker": ticker,
            "headline": headline,
            "status": STATUS_FAILED,
            "retry_count": retry_count,
            "last_attempt": last_attempt,
            "error": "test error",
        }
    }


# ---------------------------------------------------------------------------
# 1. is_high_priority_title
# ---------------------------------------------------------------------------

def test_high_priority_half_year():
    assert is_high_priority_title("NHC Half Year Results FY25") is True


def test_high_priority_h1():
    assert is_high_priority_title("1H FY2026 Results") is True


def test_high_priority_appendix_4d():
    assert is_high_priority_title("Appendix 4D and Half-Year Report") is True


def test_high_priority_appendix_4e():
    assert is_high_priority_title("Appendix 4E — Full Year Results") is True


def test_high_priority_earnings():
    assert is_high_priority_title("BHP Earnings Release FY2025") is True


def test_high_priority_guidance():
    assert is_high_priority_title("FY2025 Guidance Update") is True


def test_high_priority_results():
    assert is_high_priority_title("Annual Results Presentation") is True


def test_not_high_priority_agm():
    """AGM notices are NOT high-priority earnings events."""
    assert is_high_priority_title("Notice of Annual General Meeting") is False


def test_not_high_priority_change_of_address():
    assert is_high_priority_title("Change of Registered Address") is False


def test_high_priority_keywords_constant_populated():
    assert len(HIGH_PRIORITY_KEYWORDS) >= 7


# ---------------------------------------------------------------------------
# 2. should_process_item — full decision matrix
# ---------------------------------------------------------------------------

NHC_URL = "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=99999999"
NHC_TICKER = "NHC"
NHC_TITLE = "NHC Half Year Results FY25"
NHC_KEY = announcement_key(NHC_TICKER, NHC_URL)


def test_process_new_announcement():
    """New announcements (not in state) should always be processed."""
    ok, reason = should_process_item(NHC_KEY, NHC_TICKER, NHC_TITLE, {}, False, frozenset())
    assert ok is True
    assert "new" in reason.lower()


def test_skip_completed_normal():
    """COMPLETED non-high-priority announcements must be skipped."""
    state = _make_completed_entry(NHC_TICKER, NHC_URL, headline="Company Secretary Change")
    key = announcement_key(NHC_TICKER, NHC_URL)
    ok, reason = should_process_item(
        key, NHC_TICKER, "Company Secretary Change", state, False, frozenset()
    )
    assert ok is False
    assert "completed" in reason.lower()


def test_reprocess_completed_high_priority():
    """COMPLETED high-priority items must be reprocessed so they are never silenced."""
    state = _make_completed_entry(NHC_TICKER, NHC_URL, headline=NHC_TITLE)
    ok, reason = should_process_item(
        NHC_KEY, NHC_TICKER, NHC_TITLE, state, False, frozenset()
    )
    assert ok is True
    assert "high-priority" in reason.lower()


def test_retry_failed_within_24h():
    """FAILED items within 24 h with retry_count < MAX_RETRIES should retry."""
    state = _make_failed_entry(NHC_TICKER, NHC_URL, retry_count=1, hours_ago=2.0)
    ok, reason = should_process_item(
        NHC_KEY, NHC_TICKER, NHC_TITLE, state, False, frozenset()
    )
    assert ok is True
    assert "retrying" in reason.lower()


def test_skip_failed_max_retries():
    """FAILED items that have hit MAX_RETRIES must be skipped."""
    state = _make_failed_entry(NHC_TICKER, NHC_URL, retry_count=MAX_RETRIES, hours_ago=1.0)
    ok, reason = should_process_item(
        NHC_KEY, NHC_TICKER, NHC_TITLE, state, False, frozenset()
    )
    assert ok is False
    assert "max retries" in reason.lower()


def test_skip_failed_outside_24h_window():
    """FAILED items outside the 24 h retry window should be skipped."""
    state = _make_failed_entry(NHC_TICKER, NHC_URL, retry_count=1, hours_ago=HOURS_BACK + 2)
    ok, reason = should_process_item(
        NHC_KEY, NHC_TICKER, NHC_TITLE, state, False, frozenset()
    )
    assert ok is False
    assert "24" in reason or "window" in reason


def test_retry_processing_state():
    """PROCESSING state (crashed run) must be treated as retriable."""
    key = NHC_KEY
    state = {
        key: {
            "announcement_id": key,
            "ticker": NHC_TICKER,
            "headline": NHC_TITLE,
            "status": STATUS_PROCESSING,
            "retry_count": 0,
            "last_attempt": now_sgt().isoformat(timespec="seconds"),
            "error": "",
        }
    }
    ok, reason = should_process_item(key, NHC_TICKER, NHC_TITLE, state, False, frozenset())
    assert ok is True
    assert "processing" in reason.lower() or "crashed" in reason.lower()


def test_force_flag_overrides_completed():
    """--force must override COMPLETED state and reprocess."""
    state = _make_completed_entry(NHC_TICKER, NHC_URL, headline="Company Secretary Change")
    key = announcement_key(NHC_TICKER, NHC_URL)
    ok, reason = should_process_item(
        key, NHC_TICKER, "Company Secretary Change", state, True, frozenset()
    )
    assert ok is True
    assert "force" in reason.lower()


def test_force_tickers_overrides_completed():
    """FORCE_RERUN_TICKERS must override COMPLETED state."""
    state = _make_completed_entry(NHC_TICKER, NHC_URL, headline="Company Secretary Change")
    key = announcement_key(NHC_TICKER, NHC_URL)
    ok, reason = should_process_item(
        key, NHC_TICKER, "Company Secretary Change", state, False, frozenset({"NHC"})
    )
    assert ok is True
    assert "force" in reason.lower()


# ---------------------------------------------------------------------------
# 3. mark_state
# ---------------------------------------------------------------------------

def test_mark_state_completed():
    state: dict = {}
    mark_state(state, NHC_KEY, NHC_TICKER, NHC_TITLE, STATUS_COMPLETED)
    entry = state[NHC_KEY]
    assert entry["status"] == STATUS_COMPLETED
    assert entry["ticker"] == NHC_TICKER
    assert entry["headline"] == NHC_TITLE
    assert entry["retry_count"] == 0
    assert entry["error"] == ""


def test_mark_state_failed_increments_retry():
    """Each FAILED mark should increment retry_count by 1."""
    state: dict = {}
    mark_state(state, NHC_KEY, NHC_TICKER, NHC_TITLE, STATUS_FAILED, "API timeout")
    assert state[NHC_KEY]["retry_count"] == 1
    assert state[NHC_KEY]["error"] == "API timeout"

    mark_state(state, NHC_KEY, NHC_TICKER, NHC_TITLE, STATUS_FAILED, "PDF download failed")
    assert state[NHC_KEY]["retry_count"] == 2


def test_mark_state_completed_resets_error():
    """COMPLETED after FAILED should store empty error."""
    state: dict = {}
    mark_state(state, NHC_KEY, NHC_TICKER, NHC_TITLE, STATUS_FAILED, "timeout")
    mark_state(state, NHC_KEY, NHC_TICKER, NHC_TITLE, STATUS_COMPLETED)
    assert state[NHC_KEY]["status"] == STATUS_COMPLETED
    assert state[NHC_KEY]["error"] == ""


def test_mark_state_stores_truncated_headline():
    long_title = "A" * 500
    state: dict = {}
    mark_state(state, NHC_KEY, NHC_TICKER, long_title, STATUS_COMPLETED)
    assert len(state[NHC_KEY]["headline"]) <= 200


# ---------------------------------------------------------------------------
# 4. load_seen_state — backward compatibility
# ---------------------------------------------------------------------------

def test_load_seen_state_empty_file(tmp_path):
    p = tmp_path / "state.json"
    result = load_seen_state(p)
    assert result == {}


def test_load_seen_state_legacy_list_format(tmp_path):
    """Old format: list of key strings → all promoted to COMPLETED."""
    import json
    key = announcement_key("NHC", "https://example.com/ann1")
    p = tmp_path / "state.json"
    p.write_text(json.dumps([key]), encoding="utf-8")
    result = load_seen_state(p)
    assert key in result
    assert result[key]["status"] == STATUS_COMPLETED


def test_load_seen_state_legacy_dict_timestamp_format(tmp_path):
    """Old format: {key: iso_timestamp} → promoted to COMPLETED."""
    import json
    key = announcement_key("BHP", "https://example.com/ann2")
    ts = now_sgt().isoformat(timespec="seconds")
    p = tmp_path / "state.json"
    p.write_text(json.dumps({key: ts}), encoding="utf-8")
    result = load_seen_state(p)
    assert key in result
    assert result[key]["status"] == STATUS_COMPLETED
    assert result[key]["last_attempt"] == ts


def test_load_seen_state_new_dict_format(tmp_path):
    """New format: {key: {status, …}} loaded as-is."""
    import json
    key = announcement_key("NHC", "https://example.com/ann3")
    entry = {
        "announcement_id": key,
        "ticker": "NHC",
        "headline": "H1 Results",
        "status": STATUS_FAILED,
        "retry_count": 1,
        "last_attempt": now_sgt().isoformat(timespec="seconds"),
        "error": "text extraction failed",
    }
    p = tmp_path / "state.json"
    p.write_text(json.dumps({key: entry}), encoding="utf-8")
    result = load_seen_state(p)
    assert result[key]["status"] == STATUS_FAILED
    assert result[key]["retry_count"] == 1


# ---------------------------------------------------------------------------
# 5. prune_seen_state — new Dict[str, Dict] format
# ---------------------------------------------------------------------------

def test_prune_removes_old_entries():
    key = announcement_key("NHC", "https://example.com/ann4")
    old_ts = (now_sgt() - dt.timedelta(hours=100)).isoformat(timespec="seconds")
    state = {
        key: {
            "announcement_id": key,
            "ticker": "NHC",
            "headline": "H1 Results",
            "status": STATUS_COMPLETED,
            "retry_count": 0,
            "last_attempt": old_ts,
            "error": "",
        }
    }
    pruned = prune_seen_state(state, retention_hours=72)
    assert key not in pruned


def test_prune_keeps_recent_entries():
    key = announcement_key("NHC", "https://example.com/ann5")
    recent_ts = now_sgt().isoformat(timespec="seconds")
    state = {
        key: {
            "announcement_id": key,
            "ticker": "NHC",
            "headline": "H1 Results",
            "status": STATUS_FAILED,
            "retry_count": 2,
            "last_attempt": recent_ts,
            "error": "timeout",
        }
    }
    pruned = prune_seen_state(state, retention_hours=72)
    assert key in pruned


# ---------------------------------------------------------------------------
# 6. Mandatory test case (from problem statement):
#    NHC H1 Results — first run FAILS, second run COMPLETES
# ---------------------------------------------------------------------------

def test_nhc_h1_results_retry_scenario():
    """
    Simulate:
      - NHC Half Year results detected
      - First run: analysis fails (text extraction failed)
      - Second run: retries successfully

    Expected behaviour:
      - First run  → status = FAILED
      - Second run → status = COMPLETED
    """
    url = "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"
    ticker = "NHC"
    title = "NHC 1H FY2025 Results"
    key = announcement_key(ticker, url)
    state: dict = {}

    # --- FIRST RUN: analysis fails (e.g. text extraction failed) ---
    # Before processing: item is new → should be processed
    ok, reason = should_process_item(key, ticker, title, state, False, frozenset())
    assert ok is True, "First run: new announcement should be processed"

    # Simulate failed text extraction: mark FAILED
    mark_state(state, key, ticker, title, STATUS_FAILED, "text extraction failed")
    entry = state[key]
    assert entry["status"] == STATUS_FAILED, "First run: should be FAILED after text extraction failure"
    assert entry["retry_count"] == 1

    # --- SECOND RUN: within 24 h → retry ---
    ok, reason = should_process_item(key, ticker, title, state, False, frozenset())
    assert ok is True, "Second run: FAILED item within 24 h should be retried"
    assert "retrying" in reason.lower()

    # Simulate successful analysis: mark COMPLETED
    mark_state(state, key, ticker, title, STATUS_COMPLETED)
    entry = state[key]
    assert entry["status"] == STATUS_COMPLETED, "Second run: should be COMPLETED after success"

    # --- THIRD CHECK: now COMPLETED + high-priority → still reprocessable ---
    # (High-priority items should never be permanently silenced)
    ok, reason = should_process_item(key, ticker, title, state, False, frozenset())
    assert ok is True, "High-priority completed items should still be reprocessable"


def test_nhc_h1_results_max_retries_stops():
    """
    After MAX_RETRIES failures the item must stop being retried within 24 h.
    """
    url = "https://www.asx.com.au/asx/v2/statistics/displayAnnouncement.do?idsId=03072858"
    ticker = "NHC"
    title = "NHC 1H FY2025 Results"
    key = announcement_key(ticker, url)
    state: dict = {}

    # Simulate MAX_RETRIES failures
    for i in range(MAX_RETRIES):
        mark_state(state, key, ticker, title, STATUS_FAILED, "persistent error")

    assert state[key]["retry_count"] == MAX_RETRIES

    # Now should_process_item must say NO (max retries exhausted)
    ok, reason = should_process_item(key, ticker, title, state, False, frozenset())
    assert ok is False, "After MAX_RETRIES failures, item should not be retried"
    assert "max retries" in reason.lower()

    # But --force must still override even at max retries
    ok, reason = should_process_item(key, ticker, title, state, True, frozenset())
    assert ok is True, "--force must override even max-retries state"
