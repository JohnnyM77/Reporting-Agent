from pathlib import Path

import pytest

from src.portfolio_loader import load_portfolio


def test_load_portfolio_dict_format(tmp_path: Path):
    """Test loading portfolio from dict format (ticker: company name)."""
    tickers_file = tmp_path / "tickers.yaml"
    tickers_file.write_text(
        """
asx:
  DRO: DroneShield Limited
  BBL: Brisbane Broncos Limited
  RMD: ResMed Inc
lse:
  RR.: Rolls-Royce Holdings plc
""",
        encoding="utf-8",
    )

    portfolio = load_portfolio(str(tickers_file), "asx", ".AX")
    assert len(portfolio) == 3
    assert portfolio[0].ticker == "DRO"
    assert portfolio[0].exchange_ticker == "DRO.AX"
    assert portfolio[1].ticker == "BBL"
    assert portfolio[2].ticker == "RMD"


def test_load_portfolio_list_format(tmp_path: Path):
    """Test loading portfolio from legacy list format."""
    tickers_file = tmp_path / "tickers.yaml"
    tickers_file.write_text(
        """
asx:
  - DRO
  - BBL
  - RMD
lse:
  - RR.
""",
        encoding="utf-8",
    )

    portfolio = load_portfolio(str(tickers_file), "asx", ".AX")
    assert len(portfolio) == 3
    assert portfolio[0].ticker == "DRO"
    assert portfolio[0].exchange_ticker == "DRO.AX"


def test_load_portfolio_missing_key(tmp_path: Path):
    """Test error handling when source key doesn't exist."""
    tickers_file = tmp_path / "tickers.yaml"
    tickers_file.write_text("asx:\n  - DRO\n", encoding="utf-8")

    with pytest.raises(ValueError, match="not found"):
        load_portfolio(str(tickers_file), "nonexistent", ".AX")


def test_load_portfolio_invalid_type(tmp_path: Path):
    """Test error handling when source key has invalid type."""
    tickers_file = tmp_path / "tickers.yaml"
    tickers_file.write_text("asx: 'not a list or dict'\n", encoding="utf-8")

    with pytest.raises(ValueError, match="expected list or dict"):
        load_portfolio(str(tickers_file), "asx", ".AX")


def test_load_portfolio_with_exchange_suffix(tmp_path: Path):
    """Test that exchange suffix is correctly applied."""
    tickers_file = tmp_path / "tickers.yaml"
    tickers_file.write_text("lse:\n  RR.: Rolls-Royce\n", encoding="utf-8")

    portfolio = load_portfolio(str(tickers_file), "lse", ".L")
    assert len(portfolio) == 1
    assert portfolio[0].ticker == "RR."
    assert portfolio[0].exchange_ticker == "RR..L"
