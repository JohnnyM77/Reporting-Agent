# tests/test_sally_exclusion.py
#
# Tests verifying that passive ETFs (VAS.AX, VHY.AX, VEU.AX) are excluded
# from Sally's portfolio analysis, while still remaining in Bob's ticker list.

import sys
import types
from pathlib import Path
from unittest.mock import patch
import tempfile

import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

# Stub heavy optional dependencies so portfolio_loader can be imported in CI
for _stub in ("anthropic",):
    if _stub not in sys.modules:
        sys.modules[_stub] = types.ModuleType(_stub)

_sally_src = str(Path(__file__).parent.parent / "sunday-sally" / "src")
if _sally_src not in sys.path:
    sys.path.insert(0, _sally_src)

from portfolio_loader import load_portfolio, SALLY_EXCLUDED_TICKERS  # noqa: E402


# ---------------------------------------------------------------------------
# A. Config: exclusion set is populated
# ---------------------------------------------------------------------------

class TestSallyExclusionConfig:
    def test_excluded_tickers_contains_etfs(self):
        """SALLY_EXCLUDED_TICKERS must include all three passive ETFs."""
        assert "VAS.AX" in SALLY_EXCLUDED_TICKERS
        assert "VHY.AX" in SALLY_EXCLUDED_TICKERS
        assert "VEU.AX" in SALLY_EXCLUDED_TICKERS

    def test_excluded_tickers_is_frozenset(self):
        """SALLY_EXCLUDED_TICKERS must be a frozenset (immutable)."""
        assert isinstance(SALLY_EXCLUDED_TICKERS, frozenset)

    def test_non_etf_not_excluded(self):
        """Normal equity tickers must NOT be in the exclusion set."""
        for ticker in ("BHP.AX", "CSL.AX", "NHC.AX"):
            assert ticker not in SALLY_EXCLUDED_TICKERS, (
                f"{ticker} should not be in SALLY_EXCLUDED_TICKERS"
            )


# ---------------------------------------------------------------------------
# B. Portfolio loader: ETFs are filtered out
# ---------------------------------------------------------------------------

class TestSallyPortfolioLoader:
    def _make_tickers_yaml(self, tickers: dict, tmp_path: Path) -> Path:
        p = tmp_path / "tickers.yaml"
        p.write_text(yaml.dump({"asx": tickers}), encoding="utf-8")
        return p

    def test_etfs_excluded_from_loaded_portfolio(self, tmp_path):
        """load_portfolio must not return PortfolioCompany objects for excluded ETFs."""
        tickers = {
            "BHP": "BHP Group",
            "CSL": "CSL Limited",
            "VAS": "Vanguard Australian Shares Index ETF",
            "VHY": "Vanguard High Yield ETF",
            "VEU": "Vanguard All-World ex-US ETF",
        }
        p = self._make_tickers_yaml(tickers, tmp_path)
        portfolio = load_portfolio(source_file=str(p), source_key="asx")

        exchange_tickers = [c.exchange_ticker for c in portfolio]
        assert "VAS.AX" not in exchange_tickers, "VAS.AX must be excluded from Sally"
        assert "VHY.AX" not in exchange_tickers, "VHY.AX must be excluded from Sally"
        assert "VEU.AX" not in exchange_tickers, "VEU.AX must be excluded from Sally"

    def test_normal_equities_still_loaded(self, tmp_path):
        """Non-ETF tickers must still be returned by load_portfolio."""
        tickers = {
            "BHP": "BHP Group",
            "CSL": "CSL Limited",
            "VAS": "Vanguard Australian Shares Index ETF",
        }
        p = self._make_tickers_yaml(tickers, tmp_path)
        portfolio = load_portfolio(source_file=str(p), source_key="asx")

        exchange_tickers = [c.exchange_ticker for c in portfolio]
        assert "BHP.AX" in exchange_tickers
        assert "CSL.AX" in exchange_tickers

    def test_skip_log_emitted(self, tmp_path, capsys):
        """Skipping an ETF must emit a [sally] log line."""
        tickers = {"VHY": "Vanguard High Yield ETF"}
        p = self._make_tickers_yaml(tickers, tmp_path)
        load_portfolio(source_file=str(p), source_key="asx")

        captured = capsys.readouterr()
        assert "skipping excluded passive ETF: VHY.AX" in captured.out

    def test_all_three_etfs_produce_skip_log(self, tmp_path, capsys):
        """All three passive ETFs must produce individual skip log lines."""
        tickers = {
            "VAS": "Vanguard Australian Shares Index ETF",
            "VHY": "Vanguard High Yield ETF",
            "VEU": "Vanguard All-World ex-US ETF",
        }
        p = self._make_tickers_yaml(tickers, tmp_path)
        load_portfolio(source_file=str(p), source_key="asx")

        captured = capsys.readouterr()
        assert "skipping excluded passive ETF: VAS.AX" in captured.out
        assert "skipping excluded passive ETF: VHY.AX" in captured.out
        assert "skipping excluded passive ETF: VEU.AX" in captured.out

    def test_empty_portfolio_when_all_excluded(self, tmp_path):
        """If all tickers are ETFs, load_portfolio returns an empty list."""
        tickers = {
            "VAS": "VAS",
            "VHY": "VHY",
            "VEU": "VEU",
        }
        p = self._make_tickers_yaml(tickers, tmp_path)
        portfolio = load_portfolio(source_file=str(p), source_key="asx")
        assert portfolio == []


# ---------------------------------------------------------------------------
# C. Bob is unaffected — tickers.yaml still contains the ETFs
# ---------------------------------------------------------------------------

class TestBobUnaffectedByExclusion:
    def test_tickers_yaml_still_contains_etfs(self):
        """tickers.yaml must still include VAS, VHY, VEU so Bob monitors them."""
        tickers_path = Path(__file__).parent.parent / "tickers.yaml"
        if not tickers_path.exists():
            return  # skip in environments without the file
        data = yaml.safe_load(tickers_path.read_text(encoding="utf-8")) or {}
        asx_tickers = data.get("asx", {})
        if isinstance(asx_tickers, dict):
            keys = set(asx_tickers.keys())
        else:
            keys = set(asx_tickers)
        assert "VAS" in keys, "VAS must remain in tickers.yaml for Bob to monitor"
        assert "VHY" in keys, "VHY must remain in tickers.yaml for Bob to monitor"
        assert "VEU" in keys, "VEU must remain in tickers.yaml for Bob to monitor"
