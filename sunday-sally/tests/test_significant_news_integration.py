from unittest.mock import patch

from src.alert_engine import AlertClassification
from src.historical_multiple_analyzer import HistoricalValuationSummary
from src.price_monitor import PriceData
from src.valuation_engine import ValuationSnapshot


def _fake_price():
    return PriceData(
        ticker="BHP", company_name="BHP Group", current_price=40.0,
        high_52w=41.0, low_52w=30.0, distance_to_high=0.02, market_cap=200_000_000_000,
    )


def _fake_val():
    return ValuationSnapshot(
        trailing_pe=15.0, forward_pe=14.0, enterprise_value=1e11,
        ev_to_ebitda=8.0, price_to_sales=2.0, fcf_yield=0.05, dividend_yield=0.03,
    )


def _fake_hist():
    return HistoricalValuationSummary(
        pe_3y_avg=13.0, pe_5y_avg=12.5, pe_10y_avg=12.0, ev_ebitda_avg=7.5,
        valuation_percentile=0.85, notes=["PE above 10y average"],
    )


def _fake_alert():
    return AlertClassification(
        triggered=True, tier="Tier 3: Deep Review",
        reasons=["within 5.0% of 52-week high"],
    )


def _fake_news_item(**overrides):
    from wally.asx_news import NewsItem
    base = dict(
        ticker="BHP.AX", date="10/06/2026", time="10:00 am",
        title="Trading Halt", url="https://www.asx.com.au/a.pdf",
        category="Trading halt / suspension", price_sensitive=True,
    )
    base.update(overrides)
    return NewsItem(**base)


class FakeCompany:
    ticker = "BHP"
    exchange_ticker = "BHP.AX"


def test_process_company_wires_significant_news(tmp_path):
    """A trim candidate gets significant ASX news threaded through summary,
    the ChatGPT handoff payload, the memo, and the Claude analysis prompt."""
    from src import main as sally_main

    run_folder = tmp_path / "run"
    run_folder.mkdir()

    captured = {}

    def fake_build_handoff_payload(**kwargs):
        captured["handoff_kwargs"] = kwargs
        return {"stub": True}

    def fake_build_memo_text(**kwargs):
        captured["memo_kwargs"] = kwargs
        return "memo text"

    def fake_analyse_company(**kwargs):
        captured["claude_kwargs"] = kwargs
        return None

    with (
        patch.object(sally_main, "fetch_price_data", return_value=_fake_price()),
        patch.object(sally_main, "fetch_valuation_snapshot", return_value=_fake_val()),
        patch.object(sally_main, "summarize_history", return_value=_fake_hist()),
        patch.object(sally_main, "valuation_ratio", return_value=1.2),
        patch.object(sally_main, "classify_alert", return_value=_fake_alert()),
        patch.object(sally_main, "fetch_asx_announcements", return_value=[]),
        patch.object(
            sally_main, "fetch_news_context",
            return_value=[{"title": "Generic headline", "publisher": "Reuters"}],
        ),
        patch.object(sally_main, "save_source_documents"),
        patch.object(sally_main, "fetch_significant_news", return_value=[_fake_news_item()]),
        patch.object(sally_main, "_ASX_NEWS_AVAILABLE", True),
        patch.object(sally_main, "NEWS_LOOKBACK_DAYS", 90),
        patch.object(sally_main, "build_handoff_payload", side_effect=fake_build_handoff_payload),
        patch.object(sally_main, "save_handoff_payload"),
        patch.object(sally_main, "analyse_company", side_effect=fake_analyse_company),
        patch.object(sally_main, "_VALUE_CHART_AVAILABLE", False),
        patch.object(sally_main, "build_memo_text", side_effect=fake_build_memo_text),
        patch.object(sally_main, "save_memo"),
    ):
        result = sally_main._process_company(
            FakeCompany(), settings={}, run_folder=run_folder, tz="Asia/Singapore",
            near_high_max=0.05, thresholds={},
        )

    assert result is not None
    assert result["significant_news"][0]["title"] == "Trading Halt"
    assert result["significant_news_lookback_days"] == 90

    assert captured["handoff_kwargs"]["significant_news"] == result["significant_news"]

    memo_kwargs = captured["memo_kwargs"]
    assert memo_kwargs["significant_news"] == result["significant_news"]
    assert memo_kwargs["lookback_days"] == 90

    claude_news = captured["claude_kwargs"]["news"]
    assert "10/06/2026 — Trading Halt" in claude_news
    assert "Generic headline" in claude_news


def test_process_company_handles_no_significant_news(tmp_path):
    """No significant announcements in the window → empty list, not an error."""
    from src import main as sally_main

    run_folder = tmp_path / "run"
    run_folder.mkdir()

    with (
        patch.object(sally_main, "fetch_price_data", return_value=_fake_price()),
        patch.object(sally_main, "fetch_valuation_snapshot", return_value=_fake_val()),
        patch.object(sally_main, "summarize_history", return_value=_fake_hist()),
        patch.object(sally_main, "valuation_ratio", return_value=1.2),
        patch.object(sally_main, "classify_alert", return_value=_fake_alert()),
        patch.object(sally_main, "fetch_asx_announcements", return_value=[]),
        patch.object(sally_main, "fetch_news_context", return_value=[]),
        patch.object(sally_main, "save_source_documents"),
        patch.object(sally_main, "fetch_significant_news", return_value=[]),
        patch.object(sally_main, "_ASX_NEWS_AVAILABLE", True),
        patch.object(sally_main, "NEWS_LOOKBACK_DAYS", 90),
        patch.object(sally_main, "build_handoff_payload", return_value={}),
        patch.object(sally_main, "save_handoff_payload"),
        patch.object(sally_main, "analyse_company", return_value=None),
        patch.object(sally_main, "_VALUE_CHART_AVAILABLE", False),
        patch.object(sally_main, "build_memo_text", return_value="memo"),
        patch.object(sally_main, "save_memo"),
    ):
        result = sally_main._process_company(
            FakeCompany(), settings={}, run_folder=run_folder, tz="Asia/Singapore",
            near_high_max=0.05, thresholds={},
        )

    assert result["significant_news"] == []


def test_process_company_degrades_when_asx_news_unavailable(tmp_path):
    """If the wally.asx_news import failed, Sally still processes the ticker —
    significant_news is just an empty list, never a crash."""
    from src import main as sally_main

    run_folder = tmp_path / "run"
    run_folder.mkdir()

    with (
        patch.object(sally_main, "fetch_price_data", return_value=_fake_price()),
        patch.object(sally_main, "fetch_valuation_snapshot", return_value=_fake_val()),
        patch.object(sally_main, "summarize_history", return_value=_fake_hist()),
        patch.object(sally_main, "valuation_ratio", return_value=1.2),
        patch.object(sally_main, "classify_alert", return_value=_fake_alert()),
        patch.object(sally_main, "fetch_asx_announcements", return_value=[]),
        patch.object(sally_main, "fetch_news_context", return_value=[]),
        patch.object(sally_main, "save_source_documents"),
        patch.object(sally_main, "_ASX_NEWS_AVAILABLE", False),
        patch.object(sally_main, "build_handoff_payload", return_value={}),
        patch.object(sally_main, "save_handoff_payload"),
        patch.object(sally_main, "analyse_company", return_value=None),
        patch.object(sally_main, "_VALUE_CHART_AVAILABLE", False),
        patch.object(sally_main, "build_memo_text", return_value="memo"),
        patch.object(sally_main, "save_memo"),
    ):
        result = sally_main._process_company(
            FakeCompany(), settings={}, run_folder=run_folder, tz="Asia/Singapore",
            near_high_max=0.05, thresholds={},
        )

    assert result["significant_news"] == []
