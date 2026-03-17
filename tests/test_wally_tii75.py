# tests/test_wally_tii75.py
#
# Tests for the TII75 canonical watchlist and Wally's screening logic.

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure repo root is on path so wally package is importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from wally.watchlist_loader import load_watchlist
from wally.screening import screen_snapshot
from wally.data_fetch import PriceSnapshot, ValuationSnapshot

_TII75_PATH = _REPO_ROOT / "watchlists" / "tii75_watchlist.yaml"

_REQUIRED_TICKERS = {"POOL", "FICO", "CPRT", "2914.T"}


# ---------------------------------------------------------------------------
# TII75 watchlist loading tests
# ---------------------------------------------------------------------------

class TestTII75WatchlistLoading:
    def test_loads_exactly_30_tickers(self):
        wl = load_watchlist(_TII75_PATH, validate_tii75=True)
        assert len(wl.tickers) == 30, (
            f"Expected 30 tickers, got {len(wl.tickers)}: {wl.tickers}"
        )

    def test_pool_is_present(self):
        wl = load_watchlist(_TII75_PATH)
        assert "POOL" in wl.tickers

    def test_fico_is_present(self):
        wl = load_watchlist(_TII75_PATH)
        assert "FICO" in wl.tickers

    def test_cprt_is_present(self):
        wl = load_watchlist(_TII75_PATH)
        assert "CPRT" in wl.tickers

    def test_2914t_is_present(self):
        wl = load_watchlist(_TII75_PATH)
        assert "2914.T" in wl.tickers

    def test_all_required_canonical_members_present(self):
        wl = load_watchlist(_TII75_PATH)
        ticker_set = set(wl.tickers)
        missing = _REQUIRED_TICKERS - ticker_set
        assert not missing, f"Missing required TII75 tickers: {missing}"

    def test_watchlist_name_is_set(self):
        wl = load_watchlist(_TII75_PATH)
        assert wl.name, "Watchlist name should not be empty"

    def test_validate_tii75_raises_on_bad_list(self, tmp_path):
        """validate_tii75=True should raise when the list is incomplete."""
        bad_yaml = tmp_path / "bad_tii75.yaml"
        bad_yaml.write_text("name: TII75 Watchlist\ntickers:\n  - POOL\n  - FICO\n")
        with pytest.raises(ValueError, match="canonical validation"):
            load_watchlist(bad_yaml, validate_tii75=True)


# ---------------------------------------------------------------------------
# Screening unit tests
# ---------------------------------------------------------------------------

class TestScreenSnapshot:
    def _make_snap(self, ticker, current_price, low_52w, high_52w=None):
        return PriceSnapshot(
            ticker=ticker,
            company_name=ticker,
            current_price=current_price,
            low_52w=low_52w,
            high_52w=high_52w if high_52w is not None else current_price * 1.5,
        )

    def test_pool_near_52w_low_is_flagged(self):
        """POOL at 208.78 with 52w low of 203.80 → ~2.44% above low → flagged."""
        snap = self._make_snap("POOL", current_price=208.78, low_52w=203.80, high_52w=353.00)
        result = screen_snapshot(snap, threshold_pct=5.0)

        expected_dist = ((208.78 - 203.80) / 203.80) * 100
        assert abs(result.distance_to_low_pct - expected_dist) < 0.01, (
            f"Expected distance_to_low_pct ≈ {expected_dist:.4f}, got {result.distance_to_low_pct:.4f}"
        )
        assert result.flagged is True, "POOL should be flagged at 2.44% above 52w low"

    def test_msft_far_from_52w_low_not_flagged(self):
        """MSFT at 399.95 with 52w low of 344.79 → >5% above low → not flagged."""
        snap = self._make_snap("MSFT", current_price=399.95, low_52w=344.79)
        result = screen_snapshot(snap, threshold_pct=5.0)

        expected_dist = ((399.95 - 344.79) / 344.79) * 100
        assert abs(result.distance_to_low_pct - expected_dist) < 0.01
        assert result.flagged is False, "MSFT should NOT be flagged when >5% above 52w low"

    def test_exactly_at_threshold_is_flagged(self):
        """Ticker exactly at threshold boundary should be flagged (<=)."""
        snap = self._make_snap("TEST", current_price=105.0, low_52w=100.0)
        result = screen_snapshot(snap, threshold_pct=5.0)
        assert result.flagged is True

    def test_just_above_threshold_not_flagged(self):
        """Ticker just above threshold should NOT be flagged."""
        snap = self._make_snap("TEST", current_price=105.01, low_52w=100.0)
        result = screen_snapshot(snap, threshold_pct=5.0)
        assert result.flagged is False

    def test_invalid_52w_low_returns_unflagged(self):
        """Zero or negative 52w low → unflagged with error."""
        snap = self._make_snap("TEST", current_price=100.0, low_52w=0.0)
        result = screen_snapshot(snap, threshold_pct=5.0)
        assert result.flagged is False
        assert result.error is not None


# ---------------------------------------------------------------------------
# Claude analyst unit tests
# ---------------------------------------------------------------------------

class TestClaudeAnalyst:
    """Unit tests for wally.claude_analyst.analyse_opportunity."""

    def test_returns_none_when_no_api_key(self):
        """analyse_opportunity returns None gracefully when ANTHROPIC_API_KEY is absent."""
        from wally.claude_analyst import analyse_opportunity

        with patch.dict("os.environ", {}, clear=True):
            result = analyse_opportunity(
                ticker="CPRT",
                company_name="Copart Inc.",
                summary={"current_price": 50.0, "low_52w": 49.0, "high_52w": 70.0, "distance_to_low_pct": 2.04},
                reasons=["Trading 2.0% above 52-week low of 49.00"],
            )
        assert result is None

    def test_returns_dict_with_expected_keys_on_success(self):
        """analyse_opportunity returns dict with all five section keys when API call succeeds."""
        from wally.claude_analyst import analyse_opportunity

        fake_block = MagicMock()
        fake_block.type = "text"
        fake_block.text = (
            "VERDICT: Looks interesting.\n\n"
            "BULL CASE: Strong moat.\n\n"
            "BEAR CASE: Market risk.\n\n"
            "WHAT MUST BE TRUE: Earnings stable.\n\n"
            "RECOMMENDATION: Monitor for now."
        )
        fake_response = MagicMock()
        fake_response.content = [fake_block]

        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_response

        fake_anthropic = MagicMock()
        fake_anthropic.Anthropic.return_value = fake_client

        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.dict("sys.modules", {"anthropic": fake_anthropic}):
                result = analyse_opportunity(
                    ticker="CPRT",
                    company_name="Copart Inc.",
                    summary={"current_price": 50.0, "low_52w": 49.0},
                    reasons=["Near 52-week low"],
                )

        assert result is not None
        assert set(result.keys()) == {"verdict", "bull_case", "bear_case", "what_must_be_true", "recommendation"}
        assert "interesting" in result["verdict"].lower()
        assert result["recommendation"] == "Monitor for now."


# ---------------------------------------------------------------------------
# Valuation workbook builder unit tests
# ---------------------------------------------------------------------------

class TestValuationWorkbook:
    """Unit tests for wally.valuation_workbook.build_valuation_workbook."""

    def test_creates_xlsx_without_claude_analysis(self, tmp_path):
        """build_valuation_workbook creates a valid .xlsx when claude_analysis is None."""
        from wally.valuation_workbook import build_valuation_workbook

        out = tmp_path / "cprt_value_chart.xlsx"
        summary = {
            "company_name": "Copart Inc.",
            "ticker": "CPRT",
            "current_price": 50.0,
            "low_52w": 49.0,
            "high_52w": 70.0,
            "distance_to_low_pct": 2.04,
            "trailing_pe": 25.0,
            "forward_pe": 22.0,
            "ev_ebitda": 18.0,
            "price_to_sales": 4.5,
            "fcf_yield": 0.04,
            "dividend_yield": None,
        }
        build_valuation_workbook(out, summary=summary, history_rows=[], decision_rows=[], claude_analysis=None)

        assert out.exists()
        import openpyxl
        wb = openpyxl.load_workbook(out)
        assert "Summary Dashboard" in wb.sheetnames
        assert "Decision Framework" in wb.sheetnames
        assert "Claude AI Analysis" not in wb.sheetnames

    def test_creates_claude_sheet_when_analysis_provided(self, tmp_path):
        """build_valuation_workbook includes 'Claude AI Analysis' sheet when analysis is given."""
        from wally.valuation_workbook import build_valuation_workbook

        out = tmp_path / "orly_value_chart.xlsx"
        claude_analysis = {
            "verdict": "Looks interesting.",
            "bull_case": "Strong moat.",
            "bear_case": "Market risk.",
            "what_must_be_true": "Earnings stable.",
            "recommendation": "Monitor for now.",
        }
        build_valuation_workbook(
            out,
            summary={"ticker": "ORLY", "company_name": "O'Reilly Automotive"},
            history_rows=[],
            decision_rows=[],
            claude_analysis=claude_analysis,
        )

        assert out.exists()
        import openpyxl
        wb = openpyxl.load_workbook(out)
        assert "Claude AI Analysis" in wb.sheetnames

    def test_all_expected_sheets_present(self, tmp_path):
        """All six standard sheets are always present in the output workbook."""
        from wally.valuation_workbook import build_valuation_workbook

        out = tmp_path / "adbe_value_chart.xlsx"
        build_valuation_workbook(
            out,
            summary={"ticker": "ADBE", "company_name": "Adobe Inc."},
            history_rows=[{"period": "current", "trailing_pe": 30.0}],
            decision_rows=[{"issue": "Near 52-week low"}],
            claude_analysis=None,
        )

        import openpyxl
        wb = openpyxl.load_workbook(out)
        for expected in [
            "Summary Dashboard",
            "Historical Valuation",
            "Valuation Comparison",
            "Implied Expectations",
            "Critical Review Notes",
            "Decision Framework",
        ]:
            assert expected in wb.sheetnames, f"Missing sheet: {expected}"


# ---------------------------------------------------------------------------
# Integration: fallback workbook path registers range PNG as inline image
# ---------------------------------------------------------------------------

class TestFallbackInlineImage:
    """Verify that _process_watchlist registers the range-chart PNG as an
    inline email image when the fallback valuation workbook is used.

    This ensures TII75 companies (which have no valuations YAML config) show
    a visual chart in the email body, matching the experience for Aussie Tech
    companies that DO have a config (whose value-chart PNG is registered inline).
    """

    def test_range_png_registered_as_inline_image_for_fallback_ticker(self, tmp_path):
        """When a ticker has no valuations config, the fallback workbook is built
        AND the 52-week range chart PNG is added to inline_images so the email
        body shows a chart rather than plain text."""
        import os
        from pathlib import Path
        from unittest.mock import MagicMock, patch

        from wally.data_fetch import PriceSnapshot, ValuationSnapshot

        # ── Mock price and valuation data ───────────────────────────────────
        fake_snap = PriceSnapshot(
            ticker="CPRT",
            company_name="Copart Inc.",
            current_price=33.88,
            low_52w=33.88,
            high_52w=63.84,
        )
        fake_val_snap = ValuationSnapshot(
            trailing_pe=25.0,
            forward_pe=22.0,
            ev_to_ebitda=18.0,
            price_to_sales=4.5,
            fcf_yield=0.04,
            dividend_yield=None,
        )

        # Create a real (but tiny) PNG so path.read_bytes() in send_email succeeds
        range_png_path = tmp_path / "cprt_range.png"
        range_png_path.write_bytes(b"\x89PNG\r\n\x1a\n")  # minimal PNG header

        # Build a minimal watchlist YAML (written before patching Path methods)
        wl_yaml = tmp_path / "test_watchlist.yaml"
        wl_yaml.write_text(
            "name: Test TII75\ntickers:\n  - ticker: CPRT\n    name: Copart Inc.\n"
        )

        with (
            patch("wally.main.fetch_price_snapshot", return_value=fake_snap),
            patch("wally.main.fetch_valuation_snapshot", return_value=fake_val_snap),
            patch("wally.main.render_range_chart", return_value=range_png_path),
            patch("wally.main.render_value_vs_price_chart", return_value=(None, "No valuation config found yet for this ticker")),
            patch("wally.main._build_xlsx", side_effect=FileNotFoundError("no config")),
            patch("wally.main._build_valuation_workbook") as mock_build_wb,
            patch("wally.main._analyse_opportunity", return_value=None),
            patch("wally.main.send_email", return_value=True),
            patch("wally.main.load_email_settings"),
            patch("wally.main.build_run_context") as mock_ctx,
            patch("wally.main.write_json"),
            patch("wally.main._XLSX_BUILDER_AVAILABLE", True),
            patch("wally.main._VALUATION_WORKBOOK_AVAILABLE", True),
        ):
            # Use a write_text that does nothing for the dashboard JSON
            with patch("wally.main.Path") as mock_path_cls:
                # Make Path("docs/data/wally.json") a no-op but keep real tmp_path
                mock_dash = MagicMock()
                mock_dash.exists.return_value = False
                mock_dash.parent.mkdir.return_value = None
                mock_path_cls.side_effect = lambda *a, **kw: (
                    mock_dash if "docs/data" in str(a[0]) else Path(*a, **kw)
                )

                # Configure run context to use tmp_path as output root
                run_ctx = MagicMock()
                run_ctx.output_root = tmp_path
                run_ctx.run_dt.strftime.return_value = "260317"
                mock_ctx.return_value = run_ctx

                # No Drive upload during test
                os.environ.pop("GDRIVE_FOLDER_ID", None)

                from wally.main import _process_watchlist

                result = _process_watchlist(
                    str(wl_yaml),
                    force=True,
                    send_individual_email=False,
                    is_tii75=False,
                )

        # The range PNG must be in inline_images with the correct CID so the
        # email body renders a chart image instead of plain text.
        cid_expected = "chart_cprt"
        inline_cids = [cid for cid, _ in result.inline_images]
        assert cid_expected in inline_cids, (
            f"Expected '{cid_expected}' in inline_images {inline_cids}. "
            "The range-chart PNG must be registered inline so TII75 emails "
            "show a visual chart (not just text)."
        )

        # The xlsx must also be in attachments
        xlsx_paths = [p for p in result.attachments if Path(p).suffix == ".xlsx"]
        assert xlsx_paths, "Expected fallback xlsx to be in attachments"

        # build_valuation_workbook must have been called once
        mock_build_wb.assert_called_once()
