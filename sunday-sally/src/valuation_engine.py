from __future__ import annotations

from dataclasses import dataclass

import yfinance as yf


@dataclass
class ValuationSnapshot:
    trailing_pe: float | None
    forward_pe: float | None
    enterprise_value: float | None
    ev_to_ebitda: float | None
    price_to_sales: float | None
    fcf_yield: float | None
    dividend_yield: float | None


def fetch_valuation_snapshot(exchange_ticker: str) -> ValuationSnapshot:
    tk = yf.Ticker(exchange_ticker)
    try:
        info = tk.info or {}
    except Exception:
        info = {}

    market_cap = info.get("marketCap")
    fcf = info.get("freeCashflow")
    fcf_yield = (fcf / market_cap) if (isinstance(fcf, (int, float)) and isinstance(market_cap, (int, float)) and market_cap) else None

    return ValuationSnapshot(
        trailing_pe=info.get("trailingPE"),
        forward_pe=info.get("forwardPE"),
        enterprise_value=info.get("enterpriseValue"),
        ev_to_ebitda=info.get("enterpriseToEbitda"),
        price_to_sales=info.get("priceToSalesTrailing12Months"),
        fcf_yield=fcf_yield,
        dividend_yield=info.get("dividendYield"),
    )
