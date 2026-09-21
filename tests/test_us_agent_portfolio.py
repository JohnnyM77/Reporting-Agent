# tests/test_us_agent_portfolio.py
#
# Coverage for the changes that turn Bob USA from a single-ticker
# dispatch tool into a scheduled portfolio agent:
#
#   1. `us:` portfolio loader (reads tickers.yaml)
#   2. Per-accession seen-state dedup (state_seen_us.json)
#   3. Merge-into-same-day us.json instead of overwriting each ticker
#
# Under the daily cron, N tickers back-to-back must all land on the
# dashboard, and tomorrow's run must NOT re-emit the same 10-K.

import json
import sys
import types
from pathlib import Path
from unittest import mock

# ---------------------------------------------------------------------------
# Stubs so us_agent imports cleanly in a test env without playwright /
# anthropic. Only stub modules that don't exist in the repo; the real
# shared.pdf_llm / sgx_pdf modules ship with the repo and other tests
# import them properly — polluting sys.modules for those would break
# every other bob test that runs after this one.
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).parent.parent))

for _stub in ("anthropic", "playwright", "playwright.sync_api"):
    sys.modules.setdefault(_stub, types.ModuleType(_stub))

# us_fetch, us_classify, us_docs, sgx_pdf, shared.pdf_llm all exist in
# the repo. Import them for real — they don't drag in Playwright or
# Anthropic at import time. sgx_email is imported lazily inside
# functions we don't call in these tests.


class _StubFetchedPdf:  # noqa: D401 -- shape only
    def __init__(self, name="", path=None, source_url=""):
        self.name = name
        self.path = path
        self.source_url = source_url


import us_agent  # noqa: E402


# ---------------------------------------------------------------------------
# 1. Portfolio loader
# ---------------------------------------------------------------------------

def test_portfolio_loader_reads_us_section(tmp_path: Path):
    yaml_path = tmp_path / "tickers.yaml"
    yaml_path.write_text(
        "asx:\n  AHC: Austco\n"
        "us:\n  MSFT: Microsoft\n  NVDA: NVIDIA\n  RMD: ResMed\n",
        encoding="utf-8",
    )
    tickers = us_agent._load_us_portfolio(yaml_path)
    # Sorted alphabetically — the loader returns a deterministic order
    # so the cron reads MSFT, NVDA, RMD every morning in the same order.
    assert tickers == ["MSFT", "NVDA", "RMD"]


def test_portfolio_loader_missing_file_is_empty(tmp_path: Path):
    assert us_agent._load_us_portfolio(tmp_path / "nope.yaml") == []


def test_portfolio_loader_missing_us_section_is_empty(tmp_path: Path):
    yaml_path = tmp_path / "tickers.yaml"
    yaml_path.write_text("asx:\n  AHC: Austco\n", encoding="utf-8")
    assert us_agent._load_us_portfolio(yaml_path) == []


def test_tickers_yaml_has_rmd_under_us():
    """The real tickers.yaml must carry RMD under us: — the config
    change that motivated this whole PR. If a future edit accidentally
    moves it back to asx:, this test fails loudly."""
    real = Path(__file__).parent.parent / "tickers.yaml"
    tickers = us_agent._load_us_portfolio(real)
    assert "RMD" in tickers, "RMD must be under us: in tickers.yaml"

    import yaml
    data = yaml.safe_load(real.read_text(encoding="utf-8")) or {}
    asx = data.get("asx") or {}
    assert "RMD" not in asx, (
        "RMD reports quarterly as a US-listed entity — it must NOT "
        "be under asx: any more"
    )


# ---------------------------------------------------------------------------
# 2. Seen-state dedup
# ---------------------------------------------------------------------------

def test_seen_state_round_trip(tmp_path: Path, monkeypatch):
    path = tmp_path / "state_seen_us.json"
    monkeypatch.setattr(us_agent, "US_SEEN_STATE_PATH", path)

    state = {us_agent._seen_key("MSFT", "0000789019-25-000012"): "2026-09-20T09:13:00"}
    us_agent._save_us_seen_state(state, path)
    loaded = us_agent._load_us_seen_state(path)
    assert loaded == state


def test_seen_state_load_missing_returns_empty(tmp_path: Path):
    assert us_agent._load_us_seen_state(tmp_path / "nope.json") == {}


def test_seen_state_load_legacy_list_shape(tmp_path: Path):
    """An earlier shape might land as a plain list of accession strings.
    Roll it into today-stamped map rather than dropping it — a fresh
    write next run tidies the file up."""
    path = tmp_path / "seen.json"
    path.write_text(json.dumps(["MSFT|123", "NVDA|456"]), encoding="utf-8")
    loaded = us_agent._load_us_seen_state(path)
    assert set(loaded.keys()) == {"MSFT|123", "NVDA|456"}
    for iso in loaded.values():
        assert isinstance(iso, str) and iso


def test_seen_state_key_is_ticker_scoped():
    """Same accession number under two different issuers stays two
    distinct keys — accession collisions across CIKs don't happen in
    practice but the key shape is written to be robust anyway."""
    a = us_agent._seen_key("MSFT", "0000789019-25-000012")
    b = us_agent._seen_key("NVDA", "0000789019-25-000012")
    assert a != b
    assert a.startswith("MSFT|")
    assert b.startswith("NVDA|")


# ---------------------------------------------------------------------------
# 3. Merge write — same-day multi-ticker must coexist on the dashboard
# ---------------------------------------------------------------------------

def _analysis_row(ticker: str) -> dict:
    """Minimal analysis dict — just enough to look 'ok' to the writer."""
    return {
        "period": "Q1 FY26",
        "period_type": "quarterly",
        "currency": "USD",
        "metrics": {"revenue": {"value": "US$50bn", "change_pct": "+8%"}},
        "summary": f"{ticker} beat.",
        "full_analysis": f"## Verdict\n\n{ticker} beat.",
    }


def _fetched_pdf(name="doc.pdf", url="https://sec.gov/x") -> object:
    return _StubFetchedPdf(name=name, path=Path("/tmp/x"), source_url=url)


def test_first_ticker_creates_us_json(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(us_agent, "US_DASHBOARD_JSON", tmp_path / "us.json")
    us_agent._write_dashboard_json(
        {"ticker": "MSFT", "title": "Microsoft 10-Q Q1 FY26",
         "url": "https://sec.gov/msft", "form": "10-Q"},
        _analysis_row("MSFT"),
        [_fetched_pdf("msft-10q.pdf", "https://sec.gov/msft")],
    )
    data = json.loads((tmp_path / "us.json").read_text(encoding="utf-8"))
    assert data["last_run"]
    assert [x["ticker"] for x in data["high_impact"]] == ["MSFT"]


def test_second_ticker_same_day_appends_not_overwrites(tmp_path: Path, monkeypatch):
    """The original overwrite behaviour meant only the LAST ticker of a
    portfolio run appeared on the dashboard — MSFT, NVDA, RMD would
    collapse to just RMD. Merge appends so all three coexist."""
    monkeypatch.setattr(us_agent, "US_DASHBOARD_JSON", tmp_path / "us.json")

    for ticker in ("MSFT", "NVDA", "RMD"):
        us_agent._write_dashboard_json(
            {"ticker": ticker, "title": f"{ticker} 10-Q",
             "url": f"https://sec.gov/{ticker}"},
            _analysis_row(ticker),
            [_fetched_pdf(f"{ticker.lower()}.pdf")],
        )
    data = json.loads((tmp_path / "us.json").read_text(encoding="utf-8"))
    tickers = [x["ticker"] for x in data["high_impact"]]
    assert tickers == ["MSFT", "NVDA", "RMD"]


def test_same_ticker_re_run_replaces_not_duplicates(tmp_path: Path, monkeypatch):
    """A re-run in the same day (e.g. after a fix push) must not stack
    duplicate MSFT rows — the newer write wins."""
    monkeypatch.setattr(us_agent, "US_DASHBOARD_JSON", tmp_path / "us.json")

    us_agent._write_dashboard_json(
        {"ticker": "MSFT", "title": "Old title", "url": "https://old"},
        _analysis_row("MSFT"),
        [_fetched_pdf("old.pdf")],
    )
    us_agent._write_dashboard_json(
        {"ticker": "MSFT", "title": "New title", "url": "https://new"},
        _analysis_row("MSFT"),
        [_fetched_pdf("new.pdf")],
    )
    data = json.loads((tmp_path / "us.json").read_text(encoding="utf-8"))
    hi = data["high_impact"]
    assert len(hi) == 1
    assert hi[0]["title"] == "New title"
    assert hi[0]["url"] == "https://new"


def test_stale_us_json_from_yesterday_is_reset(tmp_path: Path, monkeypatch):
    """A file whose last_run is not today is stale — Bob USA's previous
    day's work stays in git history but the new day starts fresh."""
    path = tmp_path / "us.json"
    path.write_text(json.dumps({
        "last_run": "1999-01-01",
        "silence": False,
        "high_impact": [{"ticker": "OLD", "title": "Yesterday", "url": "x", "type": "results"}],
        "material": [],
        "fyi": [],
    }), encoding="utf-8")
    monkeypatch.setattr(us_agent, "US_DASHBOARD_JSON", path)

    us_agent._write_dashboard_json(
        {"ticker": "MSFT", "title": "Today", "url": "https://sec.gov/msft"},
        _analysis_row("MSFT"),
        [_fetched_pdf()],
    )
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [x["ticker"] for x in data["high_impact"]] == ["MSFT"]
    assert "OLD" not in json.dumps(data)


def test_ticker_with_no_analysis_lands_in_fyi(tmp_path: Path, monkeypatch):
    """The original behaviour: an item without analysis was FYI. Merge
    write must preserve that (a ticker whose EDGAR fetch found no
    fresh 10-K falls through to FYI on the dashboard)."""
    monkeypatch.setattr(us_agent, "US_DASHBOARD_JSON", tmp_path / "us.json")
    us_agent._write_dashboard_json(
        {"ticker": "MSFT", "title": "Filing", "url": "https://sec.gov/msft"},
        None,
        [_fetched_pdf()],
    )
    data = json.loads((tmp_path / "us.json").read_text(encoding="utf-8"))
    assert data["high_impact"] == []
    assert [x["ticker"] for x in data["fyi"]] == ["MSFT"]
