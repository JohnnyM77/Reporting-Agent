-- =============================================================================
-- JM Investing — Example Queries
-- =============================================================================

-- ── Import summary ────────────────────────────────────────────────────────────
SELECT * FROM v_company_import_summary;

-- ── All key scalar metrics for BHP ───────────────────────────────────────────
SELECT metric_name, value
FROM v_key_scalar_metrics
WHERE ticker = 'BHP'
ORDER BY metric_name;

-- ── EPS history (actual, from value_merged_future_earnings_per_share) ─────────
SELECT * FROM v_eps_history WHERE ticker = 'WTC' ORDER BY date_utc;

-- ── Analyst EPS forecasts (consensus + high/low band) ─────────────────────────
SELECT ticker, metric_name, date_utc, eps
FROM v_eps_forecasts
ORDER BY ticker, date_utc, metric_name;

-- ── Dividend history for all companies ───────────────────────────────────────
SELECT * FROM v_dividend_history ORDER BY ticker, date_utc;

-- ── Screener: high-quality companies (ROE > 15%, Debt/Equity < 0.5) ──────────
SELECT
    e.ticker,
    MAX(CASE WHEN e.metric_name = 'past_return_on_equity'     THEN e.value END) AS roe,
    MAX(CASE WHEN e.metric_name = 'health_debt_to_equity_ratio' THEN e.value END) AS d_e,
    MAX(CASE WHEN e.metric_name = 'value_pe'                   THEN e.value END) AS pe,
    MAX(CASE WHEN e.metric_name = 'value_intrinsic_discount'   THEN e.value END) AS discount
FROM v_key_scalar_metrics e
GROUP BY e.ticker
HAVING roe > 0.15 AND (d_e < 0.5 OR d_e IS NULL)
ORDER BY roe DESC;

-- ── Future EPS growth — consensus estimate ────────────────────────────────────
SELECT ticker, value AS eps_growth_1y
FROM sws_scalar_metrics
WHERE metric_name = 'future_earnings_per_share_growth_1y'
ORDER BY value DESC;

-- ── Revenue trend for WTC (trailing LTM history) ──────────────────────────────
SELECT ticker, date_utc, value AS revenue_m
FROM sws_timeseries
WHERE ticker = 'WTC'
  AND metric_name = 'past_revenue_ltm_history'
ORDER BY date_utc;

-- ── Levered free cash flow trend ──────────────────────────────────────────────
SELECT ticker, date_utc, value AS fcf_m
FROM sws_timeseries
WHERE metric_name = 'health_levered_free_cash_flow_history'
ORDER BY ticker, date_utc;

-- ── Latest share prices ───────────────────────────────────────────────────────
SELECT ticker, value AS price
FROM sws_scalar_metrics
WHERE metric_name = 'value_last_share_price'
ORDER BY ticker;

-- ── Price vs intrinsic value (discount = positive means undervalued) ──────────
SELECT
    p.ticker,
    p.value AS price,
    iv.value AS intrinsic_value_per_share,
    d.value AS intrinsic_discount,
    ROUND((iv.value - p.value) / p.value * 100, 1) AS upside_pct
FROM sws_scalar_metrics p
JOIN sws_scalar_metrics iv ON iv.ticker = p.ticker AND iv.metric_name = 'value_intrinsic_value_npv_per_share'
JOIN sws_scalar_metrics d  ON d.ticker  = p.ticker AND d.metric_name  = 'value_intrinsic_discount'
WHERE p.metric_name = 'value_last_share_price'
ORDER BY upside_pct DESC;

-- ── Operating expense trend for MVP ──────────────────────────────────────────
SELECT ticker, date_utc, value AS opex_m
FROM sws_timeseries
WHERE ticker = 'MVP'
  AND metric_name = 'health_operating_expenses_total'
ORDER BY date_utc;

-- ── All metrics available for a given ticker ─────────────────────────────────
SELECT metric_name, value, raw_value
FROM sws_scalar_metrics
WHERE ticker = 'DRO'
ORDER BY metric_name;

-- ── All timeseries metrics available for a ticker ────────────────────────────
SELECT DISTINCT metric_name
FROM sws_timeseries
WHERE ticker = 'DRO'
ORDER BY metric_name;

-- ── Data freshness check ──────────────────────────────────────────────────────
SELECT ticker, last_imported_at, status
FROM v_company_import_summary
ORDER BY last_imported_at DESC;
