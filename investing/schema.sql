-- =============================================================================
-- JM Investing — Simply Wall St SQLite Schema
-- =============================================================================
-- Central data warehouse for Bob, Wally and Sally.
-- Source: Simply Wall St CSV exports (one file per ticker, e.g. ASX_BHP.csv)
--
-- Import pipeline:
--   import_sws_csv.py → sws_raw_rows (raw preservation)
--                     → sws_scalar_metrics (single-value metrics)
--                     → sws_timeseries (_date + value row pairs)
--
-- YAML overrides in valuations/ always win over any API data (Wally-specific).
-- =============================================================================

-- -----------------------------------------------------------------------------
-- companies
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS companies (
    ticker              TEXT PRIMARY KEY,
    exchange            TEXT NOT NULL DEFAULT 'ASX',
    source              TEXT NOT NULL DEFAULT 'Simply Wall St',
    first_imported_at   TEXT,           -- ISO-8601 UTC
    last_imported_at    TEXT            -- ISO-8601 UTC
);

-- -----------------------------------------------------------------------------
-- import_log
-- One row per file import. file_hash prevents duplicate imports.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS import_log (
    import_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT    NOT NULL,
    source_file         TEXT    NOT NULL,
    file_hash           TEXT    NOT NULL,   -- SHA-256 of raw CSV bytes
    imported_at         TEXT    NOT NULL,   -- ISO-8601 UTC
    raw_rows            INTEGER NOT NULL DEFAULT 0,
    scalar_rows         INTEGER NOT NULL DEFAULT 0,
    timeseries_rows     INTEGER NOT NULL DEFAULT 0,
    status              TEXT    NOT NULL DEFAULT 'ok',  -- ok | warning | error
    warning             TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_import_log_hash
    ON import_log (file_hash);

-- -----------------------------------------------------------------------------
-- sws_raw_rows — verbatim preservation of every row from the CSV
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sws_raw_rows (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    metric_name     TEXT    NOT NULL,
    value_index     INTEGER NOT NULL,   -- 0-based column index within the value columns
    raw_value       TEXT,               -- exactly as it appeared in the CSV
    import_id       INTEGER NOT NULL REFERENCES import_log(import_id),
    source_file     TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_raw_ticker_metric
    ON sws_raw_rows (ticker, metric_name);

-- -----------------------------------------------------------------------------
-- sws_scalar_metrics — metrics with a single numeric or text value
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sws_scalar_metrics (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    metric_name     TEXT    NOT NULL,
    value           REAL,               -- NULL when not numeric
    raw_value       TEXT,
    import_id       INTEGER NOT NULL REFERENCES import_log(import_id),
    source_file     TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_scalar_ticker_metric
    ON sws_scalar_metrics (ticker, metric_name);

CREATE INDEX IF NOT EXISTS ix_scalar_ticker
    ON sws_scalar_metrics (ticker);

-- -----------------------------------------------------------------------------
-- sws_timeseries — paired _date + value rows, one row per (ticker, metric, date)
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sws_timeseries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT    NOT NULL,
    metric_name     TEXT    NOT NULL,   -- without the _date suffix
    date_unix       INTEGER NOT NULL,   -- original Unix timestamp (ms assumed if > 1e10)
    date_utc        TEXT    NOT NULL,   -- YYYY-MM-DD
    value           REAL,
    raw_value       TEXT,
    value_index     INTEGER,            -- 0-based position within the series
    import_id       INTEGER NOT NULL REFERENCES import_log(import_id),
    source_file     TEXT    NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_ts_ticker_metric_date
    ON sws_timeseries (ticker, metric_name, date_unix);

CREATE INDEX IF NOT EXISTS ix_ts_ticker_metric
    ON sws_timeseries (ticker, metric_name);

-- =============================================================================
-- VIEWS
-- =============================================================================

DROP VIEW IF EXISTS v_company_import_summary;
CREATE VIEW v_company_import_summary AS
SELECT
    c.ticker,
    c.exchange,
    c.last_imported_at,
    l.source_file,
    l.raw_rows,
    l.scalar_rows,
    l.timeseries_rows,
    l.status
FROM companies c
JOIN import_log l ON l.ticker = c.ticker
WHERE l.import_id IN (
    SELECT MAX(import_id) FROM import_log GROUP BY ticker
);

-- Key metrics most useful for screening and analysis
DROP VIEW IF EXISTS v_key_scalar_metrics;
CREATE VIEW v_key_scalar_metrics AS
SELECT ticker, metric_name, value
FROM sws_scalar_metrics
WHERE metric_name IN (
    -- Valuation
    'value_pe',
    'value_pb',
    'value_price_to_sales',
    'value_ev_to_ebitda',
    'value_intrinsic_discount',
    'value_npv_per_share',
    'value_last_share_price',
    'value_market_cap',
    'value_price_target',
    'value_price_target_low',
    'value_price_target_high',
    -- Past performance
    'past_earnings_per_share',
    'past_earnings_per_share_1y',
    'past_earnings_per_share_3y',
    'past_earnings_per_share_5y',
    'past_earnings_per_share_growth_1y',
    'past_revenue',
    'past_revenue_growth_1y',
    'past_revenue_growth_3y',
    'past_return_on_equity',
    'past_return_on_assets',
    'past_net_income_margin',
    'past_ebit_margin',
    'past_gross_profit_margin',
    -- Future / analyst estimates
    'future_earnings_per_share_1y',
    'future_earnings_per_share_2y',
    'future_earnings_per_share_3y',
    'future_earnings_per_share_growth_1y',
    'future_earnings_per_share_growth_3y',
    'future_revenue_1y',
    'future_revenue_growth_1y',
    'future_revenue_growth_3y',
    'future_return_on_equity_1y',
    'future_return_on_equity_3y',
    -- Dividends
    'dividend_dividend_yield',
    'dividend_dividend_yield_future',
    'dividend_payout_ratio',
    'dividend_buyback_yield',
    'dividend_total_shareholder_yield',
    -- Health / balance sheet
    'health_debt_to_equity_ratio',
    'health_net_debt_to_equity',
    'health_current_solvency_ratio',
    'health_net_interest_cover',
    'health_total_debt',
    'health_total_equity',
    'health_total_assets',
    'health_cash_st_investments',
    'health_levered_free_cash_flow'
);

-- Convenience: latest EPS timeseries per ticker (annual, from value_ prefix)
DROP VIEW IF EXISTS v_eps_history;
CREATE VIEW v_eps_history AS
SELECT
    ticker,
    date_utc,
    value AS eps
FROM sws_timeseries
WHERE metric_name = 'value_merged_future_earnings_per_share'
ORDER BY ticker, date_utc;

-- Convenience: analyst consensus EPS forecasts (future_ prefix)
DROP VIEW IF EXISTS v_eps_forecasts;
CREATE VIEW v_eps_forecasts AS
SELECT
    ticker,
    metric_name,
    date_utc,
    value AS eps
FROM sws_timeseries
WHERE metric_name IN (
    'future_merged_future_earnings_per_share',
    'future_merged_future_earnings_per_share_high',
    'future_merged_future_earnings_per_share_low'
)
ORDER BY ticker, metric_name, date_utc;

-- Convenience: dividend payment history
DROP VIEW IF EXISTS v_dividend_history;
CREATE VIEW v_dividend_history AS
SELECT
    ticker,
    date_utc,
    value AS dividend_per_share
FROM sws_timeseries
WHERE metric_name = 'dividend_historical_dividend_payments'
ORDER BY ticker, date_utc;
