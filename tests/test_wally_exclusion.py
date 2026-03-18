# tests/test_wally_exclusion.py
#
# Tests verifying that passive ETFs (VAS.AX, VHY.AX, VEU.AX) are excluded
# from Wally screening even when they appear in a watchlist file.

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile
import yaml

# ---------------------------------------------------------------------------
# Stub heavy optional dependencies so wally modules can be imported in CI
# ---------------------------------------------------------------------------
for _stub in (
    "anthropic", "openpyxl", "openpyxl.styles", "openpyxl.utils",
    "matplotlib", "matplotlib.pyplot", "matplotlib.figure",
    "matplotlib.axes", "matplotlib.patches", "matplotlib.lines",
    "matplotlib.ticker", "yfinance", "requests", "pandas",
):
    if _stub not in sys.modules:
        sys.modules[_stub] = types.ModuleType(_stub)

sys.path.insert(0, str(Path(__file__).parent.parent))

from wally.config import WALLY_EXCLUDED_TICKERS  # noqa: E402


# ---------------------------------------------------------------------------
# A. Config: exclusion set is populated
# ---------------------------------------------------------------------------

class TestWallyExclusionConfig:
    def test_excluded_tickers_contains_etfs(self):
        """WALLY_EXCLUDED_TICKERS must include all three passive ETFs."""
        assert "VAS.AX" in WALLY_EXCLUDED_TICKERS
        assert "VHY.AX" in WALLY_EXCLUDED_TICKERS
        assert "VEU.AX" in WALLY_EXCLUDED_TICKERS

    def test_excluded_tickers_is_frozenset(self):
        """WALLY_EXCLUDED_TICKERS must be a frozenset (immutable)."""
        assert isinstance(WALLY_EXCLUDED_TICKERS, frozenset)

    def test_non_etf_not_excluded(self):
        """Normal equity tickers must NOT be in the exclusion set."""
        for ticker in ("BHP.AX", "CSL.AX", "NHC.AX", "BHP", "CSL"):
            assert ticker not in WALLY_EXCLUDED_TICKERS, (
                f"{ticker} should not be in WALLY_EXCLUDED_TICKERS"
            )


# ---------------------------------------------------------------------------
# B. Watchlist loader: normalisation
# ---------------------------------------------------------------------------

class TestWatchlistLoaderExclusion:
    def _make_watchlist_yaml(self, tickers: list, tmp_path: Path) -> Path:
        p = tmp_path / "test_watchlist.yaml"
        p.write_text(yaml.dump({"name": "Test", "tickers": tickers}), encoding="utf-8")
        return p

    def test_excluded_etfs_are_upper_in_exclusion_set(self, tmp_path):
        """Exclusion lookup is case-insensitive via upper() normalisation."""
        # The exclusion set stores upper-case; watchlist_loader already normalises
        # to upper case, so "vas.ax" in a YAML is seen as "VAS.AX".
        assert "VAS.AX" in WALLY_EXCLUDED_TICKERS
        assert "vas.ax" not in WALLY_EXCLUDED_TICKERS  # stored as upper

    def test_load_watchlist_does_not_contain_excluded_etfs(self, tmp_path):
        """Loading a watchlist that includes ETFs should return them normalised,
        and _process_watchlist is responsible for skipping them."""
        from wally.watchlist_loader import load_watchlist
        tickers_in_file = ["BHP.AX", "VAS.AX", "VHY.AX", "CSL.AX", "VEU.AX"]
        p = self._make_watchlist_yaml(tickers_in_file, tmp_path)
        wl = load_watchlist(str(p))
        # Loader normalises to upper; all five present before exclusion filter
        assert "BHP.AX" in wl.tickers
        assert "VAS.AX" in wl.tickers
        assert "VHY.AX" in wl.tickers
        assert "VEU.AX" in wl.tickers


# ---------------------------------------------------------------------------
# C. _process_watchlist: ETFs are skipped, normals are processed
# ---------------------------------------------------------------------------

class TestProcessWatchlistExclusion:
    def test_etfs_skipped_in_process_watchlist(self, tmp_path, capsys):
        """_process_watchlist must skip VAS.AX, VHY.AX, VEU.AX."""
        import datetime as dt
        from wally.watchlist_loader import Watchlist
        from wally import main as wally_main

        # Build a tiny watchlist with one real ticker + three ETFs
        wl_path = tmp_path / "test_wl.yaml"
        wl_path.write_text(
            yaml.dump({"name": "Test", "tickers": ["BHP.AX", "VAS.AX", "VHY.AX", "VEU.AX"]}),
            encoding="utf-8",
        )

        processed_tickers = []

        def fake_fetch_price(ticker):
            processed_tickers.append(ticker)
            return None  # returning None causes the ticker to be added as "No market data"

        with patch.object(wally_main, "fetch_price_snapshot", side_effect=fake_fetch_price), \
             patch.object(wally_main, "build_run_context") as mock_ctx, \
             patch.object(wally_main, "load_watchlist", return_value=Watchlist(
                 name="Test",
                 tickers=["BHP.AX", "VAS.AX", "VHY.AX", "VEU.AX"],
                 source_path=wl_path,
             )):
            mock_ctx.return_value.output_root = tmp_path
            mock_ctx.return_value.run_dt = dt.datetime(2025, 1, 1, 0, 0, 0)

            result = wally_main._process_watchlist(str(wl_path), force=False, send_individual_email=False)

        # Only BHP.AX should have been fetched — the three ETFs must have been skipped
        assert "BHP.AX" in processed_tickers
        assert "VAS.AX" not in processed_tickers, "VAS.AX must be skipped by Wally"
        assert "VHY.AX" not in processed_tickers, "VHY.AX must be skipped by Wally"
        assert "VEU.AX" not in processed_tickers, "VEU.AX must be skipped by Wally"

    def test_skip_log_message_printed(self, tmp_path, capsys):
        """Skipping an ETF must emit a [wally] log line."""
        import datetime as dt
        from wally.watchlist_loader import Watchlist
        from wally import main as wally_main

        wl_path = tmp_path / "test_wl.yaml"
        wl_path.write_text(
            yaml.dump({"name": "Test", "tickers": ["VAS.AX"]}),
            encoding="utf-8",
        )

        with patch.object(wally_main, "fetch_price_snapshot", return_value=None), \
             patch.object(wally_main, "build_run_context") as mock_ctx, \
             patch.object(wally_main, "load_watchlist", return_value=Watchlist(
                 name="Test",
                 tickers=["VAS.AX"],
                 source_path=wl_path,
             )):
            mock_ctx.return_value.output_root = tmp_path
            mock_ctx.return_value.run_dt = dt.datetime(2025, 1, 1, 0, 0, 0)

            wally_main._process_watchlist(str(wl_path), force=False, send_individual_email=False)

        captured = capsys.readouterr()
        assert "skipping excluded passive ETF: VAS.AX" in captured.out

    def test_no_xlsx_generated_for_etf(self, tmp_path, capsys):
        """No valuation workbook must be generated for excluded ETFs."""
        import datetime as dt
        from wally.watchlist_loader import Watchlist
        from wally import main as wally_main

        wl_path = tmp_path / "test_wl.yaml"
        wl_path.write_text(
            yaml.dump({"name": "Test", "tickers": ["VAS.AX"]}),
            encoding="utf-8",
        )

        xlsx_calls = []
        original_builder = wally_main._XLSX_BUILDER_AVAILABLE

        try:
            wally_main._XLSX_BUILDER_AVAILABLE = True

            def fake_build_xlsx(ticker, **kwargs):
                xlsx_calls.append(ticker)
                return tmp_path / f"{ticker}_value_chart.xlsx"

            with patch.object(wally_main, "_build_xlsx", fake_build_xlsx, create=True), \
                 patch.object(wally_main, "fetch_price_snapshot", return_value=None), \
                 patch.object(wally_main, "build_run_context") as mock_ctx, \
                 patch.object(wally_main, "load_watchlist", return_value=Watchlist(
                     name="Test",
                     tickers=["VAS.AX"],
                     source_path=wl_path,
                 )):
                mock_ctx.return_value.output_root = tmp_path
                mock_ctx.return_value.run_dt = dt.datetime(2025, 1, 1, 0, 0, 0)
                wally_main._process_watchlist(str(wl_path), force=False, send_individual_email=False)
        finally:
            wally_main._XLSX_BUILDER_AVAILABLE = original_builder

        assert "VAS.AX" not in xlsx_calls, "No XLSX workbook should be built for VAS.AX"
