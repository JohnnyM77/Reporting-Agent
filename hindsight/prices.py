"""Price data. yfinance directly, the same library Wally and Sally use.

Wally's ``fetch_price_snapshot`` only returns a one-year snapshot, which is
not enough for "daily move" or "since purchase", so this is a thin sibling
rather than a reuse. Every failure returns None; a missing price never
stops a run, it just means a gate that needs it cannot fire.
"""

from __future__ import annotations


class PriceProvider:
    def recent(self, tickers: list[str]) -> dict[str, dict]:
        """``{ticker: {"last": float, "prev": float}}`` for bare ASX tickers."""
        raise NotImplementedError

    def current(self, ticker: str) -> float | None:
        return (self.recent([ticker]).get(ticker) or {}).get("last")


class StaticPrices(PriceProvider):
    def __init__(self, data: dict[str, dict] | None = None):
        self.data = data or {}

    def recent(self, tickers):
        return {t: self.data[t] for t in tickers if t in self.data}


class YFinancePrices(PriceProvider):
    def __init__(self, suffix: str = ".AX"):
        self.suffix = suffix
        self._cache: dict[str, dict] = {}

    def recent(self, tickers):
        want = [t for t in tickers if t not in self._cache]
        if want:
            try:
                import yfinance as yf

                data = yf.download([t + self.suffix for t in want], period="7d", interval="1d",
                                   auto_adjust=False, progress=False, group_by="ticker", threads=True)
                for t in want:
                    try:
                        frame = data[t + self.suffix] if len(want) > 1 else data
                        closes = [float(c) for c in frame["Close"].dropna().tolist()]
                    except Exception:
                        continue
                    if len(closes) >= 2:
                        self._cache[t] = {"last": closes[-1], "prev": closes[-2]}
                    elif closes:
                        self._cache[t] = {"last": closes[-1], "prev": None}
            except Exception as exc:  # network, rate limit, missing package
                print(f"[hindsight] price fetch failed ({type(exc).__name__}); price gates will not fire")
        return {t: self._cache[t] for t in tickers if t in self._cache}
