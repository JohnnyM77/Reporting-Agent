"""
investing/tests/test_import.py
Tests for the SWS CSV importer and reader.
"""

import hashlib
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

import pytest

# Make the repo root importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from investing.import_sws_csv import (
    _infer_ticker_exchange,
    _to_float,
    _unix_to_utc,
    import_file,
    open_db,
    parse_sws_csv,
)
from investing.sws_reader import SWSReader

FIXTURES = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_to_float_numeric(self):
        assert _to_float("3.14") == pytest.approx(3.14)

    def test_to_float_empty(self):
        assert _to_float("") is None
        assert _to_float(None) is None

    def test_to_float_non_numeric(self):
        assert _to_float("N/A") is None
        assert _to_float("AUD") is None

    def test_unix_to_utc_seconds(self):
        # 2016-02-17 = 1455667200
        assert _unix_to_utc(1455667200) == "2016-02-17"

    def test_unix_to_utc_milliseconds(self):
        assert _unix_to_utc(1455667200000) == "2016-02-17"

    def test_infer_ticker_exchange_prefixed(self):
        assert _infer_ticker_exchange("ASX_BHP.csv") == ("BHP", "ASX")
        assert _infer_ticker_exchange("NYSE_AAPL.csv") == ("AAPL", "NYSE")

    def test_infer_ticker_exchange_bare(self):
        ticker, exchange = _infer_ticker_exchange("BHP.csv")
        assert ticker == "BHP"
        assert exchange == "ASX"


# ---------------------------------------------------------------------------
# Parsing tests (require fixture CSVs)
# ---------------------------------------------------------------------------

@pytest.fixture
def bhp_csv():
    # Use the uploaded sample if available, otherwise skip
    candidates = [
        FIXTURES / "ASX_BHP.csv",
        Path("/root/.claude/uploads/e1f6f627-88aa-5114-abcd-a40c1c6ead97/5f629115-ASX_BHP.csv"),
    ]
    for p in candidates:
        if p.exists():
            return p
    pytest.skip("ASX_BHP.csv fixture not found")


@pytest.fixture
def mvp_csv():
    candidates = [
        FIXTURES / "ASX_MVP.csv",
        Path("/root/.claude/uploads/e1f6f627-88aa-5114-abcd-a40c1c6ead97/53ebad28-ASX_MVP.csv"),
    ]
    for p in candidates:
        if p.exists():
            return p
    pytest.skip("ASX_MVP.csv fixture not found")


class TestParseSWSCSV:
    def test_bhp_parse(self, bhp_csv):
        result = parse_sws_csv(bhp_csv)
        assert result["ticker"] == "BHP"
        assert result["exchange"] == "ASX"
        assert len(result["raw_rows"]) > 100
        assert len(result["scalar_metrics"]) > 50
        assert len(result["timeseries"]) > 50

    def test_timeseries_dates_are_iso(self, bhp_csv):
        result = parse_sws_csv(bhp_csv)
        for _, _, date_utc, _, _, _ in result["timeseries"]:
            assert len(date_utc) == 10, f"Bad date: {date_utc}"
            assert date_utc[4] == "-" and date_utc[7] == "-"

    def test_timeseries_dividend_payments(self, mvp_csv):
        result = parse_sws_csv(mvp_csv)
        ts_metrics = {t[0] for t in result["timeseries"]}
        assert "dividend_historical_dividend_payments" in ts_metrics

    def test_no_timeseries_for_date_rows(self, bhp_csv):
        result = parse_sws_csv(bhp_csv)
        # No metric ending in _date should appear in timeseries metric_name
        ts_names = {t[0] for t in result["timeseries"]}
        assert not any(n.endswith("_date") for n in ts_names)


# ---------------------------------------------------------------------------
# Database import tests
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db():
    with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
        path = Path(f.name)
    yield path
    path.unlink(missing_ok=True)


class TestImportFile:
    def test_import_bhp(self, tmp_db, bhp_csv):
        con = open_db(tmp_db)
        result = import_file(con, bhp_csv)
        con.close()
        assert result["status"] == "ok"
        assert result["scalar_rows"] > 50
        assert result["timeseries_rows"] > 50

    def test_no_duplicate_import(self, tmp_db, bhp_csv):
        con = open_db(tmp_db)
        import_file(con, bhp_csv)
        result2 = import_file(con, bhp_csv)
        con.close()
        assert result2["status"] == "skipped"

    def test_force_reimport(self, tmp_db, bhp_csv):
        con = open_db(tmp_db)
        import_file(con, bhp_csv)
        result2 = import_file(con, bhp_csv, force=True)
        con.close()
        assert result2["status"] == "ok"

    def test_dry_run_no_writes(self, tmp_db, bhp_csv):
        con = open_db(tmp_db)
        import_file(con, bhp_csv, dry_run=True)
        con.close()
        con2 = open_db(tmp_db)
        count = con2.execute("SELECT COUNT(*) FROM import_log").fetchone()[0]
        con2.close()
        assert count == 0

    def test_company_row_created(self, tmp_db, bhp_csv):
        con = open_db(tmp_db)
        import_file(con, bhp_csv)
        row = con.execute("SELECT ticker, exchange FROM companies WHERE ticker='BHP'").fetchone()
        con.close()
        assert row is not None
        assert row[0] == "BHP"
        assert row[1] == "ASX"

    def test_scalar_metrics_stored(self, tmp_db, bhp_csv):
        con = open_db(tmp_db)
        import_file(con, bhp_csv)
        row = con.execute(
            "SELECT value FROM sws_scalar_metrics WHERE ticker='BHP' AND metric_name='value_pe'"
        ).fetchone()
        con.close()
        assert row is not None
        assert row[0] is not None

    def test_timeseries_stored(self, tmp_db, bhp_csv):
        con = open_db(tmp_db)
        import_file(con, bhp_csv)
        count = con.execute(
            "SELECT COUNT(*) FROM sws_timeseries WHERE ticker='BHP'"
        ).fetchone()[0]
        con.close()
        assert count > 0


# ---------------------------------------------------------------------------
# SWSReader tests
# ---------------------------------------------------------------------------

@pytest.fixture
def loaded_db(tmp_db, bhp_csv, mvp_csv):
    con = open_db(tmp_db)
    import_file(con, bhp_csv)
    import_file(con, mvp_csv)
    con.close()
    return tmp_db


class TestSWSReader:
    def test_is_available(self, loaded_db):
        reader = SWSReader(loaded_db)
        assert reader.is_available()

    def test_tickers(self, loaded_db):
        with SWSReader(loaded_db) as r:
            tickers = r.tickers()
        assert "BHP" in tickers
        assert "MVP" in tickers

    def test_get_scalar(self, loaded_db):
        with SWSReader(loaded_db) as r:
            pe = r.get_scalar("BHP", "value_pe")
        assert pe is not None

    def test_get_eps_history(self, loaded_db):
        with SWSReader(loaded_db) as r:
            eps = r.get_eps_history("BHP")
        assert len(eps) > 0
        for yr, val in eps.items():
            assert len(yr) == 4
            assert isinstance(val, float)

    def test_get_eps_forecasts(self, loaded_db):
        with SWSReader(loaded_db) as r:
            forecasts = r.get_eps_forecasts("BHP")
        assert len(forecasts) > 0
        for dt, vals in forecasts.items():
            assert "consensus" in vals

    def test_get_dividend_history(self, loaded_db):
        with SWSReader(loaded_db) as r:
            divs = r.get_dividend_history("MVP")
        # MVP paid dividends historically
        assert len(divs) > 0

    def test_get_eps_cents_for_fundamentals(self, loaded_db):
        with SWSReader(loaded_db) as r:
            eps_cents, div_cents = r.get_eps_cents_for_fundamentals("BHP")
        assert len(eps_cents) > 0
        # EPS in cents should be ~100x the dollar value
        for yr, c in eps_cents.items():
            assert abs(c) < 10000  # sanity: < $100 EPS

    def test_missing_db_raises(self, tmp_path):
        reader = SWSReader(tmp_path / "nonexistent.sqlite")
        assert not reader.is_available()
        with pytest.raises(FileNotFoundError):
            reader.tickers()
