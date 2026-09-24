# shared/securities.py
#
# Canonical security identity. One place that knows "NHC" and "NHC.AX" are
# the same ASX security, that "RR." is Rolls-Royce on the LSE, that
# "ASX:ABB" is how the decision workbook writes ABB, and what Yahoo calls
# each of them.
#
# This is identity only. Which exchange a list belongs to, what is in a
# watchlist and how a position is sized stay with the agents and their data
# files. Nothing here reads a file or touches the network.
#
# Exchanges are short codes: ASX, LSE, SGX, US. An unrecognised Yahoo
# suffix (".T" Tokyo, ".TO" Toronto, ...) keeps the suffix itself as the
# exchange code, so it round-trips to the same Yahoo symbol.
#
# Units are not identity: UK names are quoted in pence (GBX) on their home
# exchange, and Wally's buy prices for them are pence too (see CLAUDE.md).
# ``quote_currency`` states that, so callers don't have to remember it.

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

ASX, LSE, SGX, US = "ASX", "LSE", "SGX", "US"

# exchange -> Yahoo Finance suffix
YAHOO_SUFFIX = {ASX: ".AX", LSE: ".L", SGX: ".SI", US: ""}
_SUFFIX_TO_EXCHANGE = {"AX": ASX, "L": LSE, "SI": SGX}
# The prefix form used by the decision workbook / broker exports ("ASX:ABB").
_PREFIX_TO_EXCHANGE = {
    "ASX": ASX, "LSE": LSE, "LON": LSE, "SGX": SGX,
    "NYSE": US, "NASDAQ": US, "NYSEARCA": US, "BATS": US, "US": US,
}
QUOTE_CURRENCY = {ASX: "AUD", LSE: "GBX", SGX: "SGD", US: "USD"}


@dataclass(frozen=True)
class Security:
    code: str       # exchange-native code, upper case, no suffix: "NHC", "RR", "D05", "POOL"
    exchange: str   # ASX / LSE / SGX / US, or a raw Yahoo suffix code for anything else

    @property
    def yahoo(self) -> str:
        """Yahoo Finance symbol: NHC.AX, RR.L, D05.SI, POOL."""
        suffix = YAHOO_SUFFIX.get(self.exchange)
        if suffix is None:
            suffix = f".{self.exchange}"
        return f"{self.code}{suffix}"

    @property
    def key(self) -> str:
        """Stable identity key, e.g. ``ASX:NHC``. Equal keys = same security."""
        return f"{self.exchange}:{self.code}"

    @property
    def asx_code(self) -> Optional[str]:
        """The bare ASX code, or None for a non-ASX security."""
        return self.code if self.exchange == ASX else None

    @property
    def quote_currency(self) -> Optional[str]:
        """The unit prices are quoted in on the home exchange (GBX = pence)."""
        return QUOTE_CURRENCY.get(self.exchange)

    def __str__(self) -> str:
        return self.yahoo


def parse_security(raw: object, default_exchange: str = ASX) -> Security:
    """Parse any ticker spelling the repo uses into a Security.

    ``NHC``, ``nhc``, ``NHC.AX`` and ``ASX:NHC`` -> ASX:NHC
    ``RR.`` (tickers.yaml LSE style), ``RR.L``, ``LSE:RR.`` -> LSE:RR
    ``D05.SI``, ``SGX:D05`` -> SGX:D05
    A bare code takes *default_exchange* (ASX unless told otherwise: every
    bare code in tickers.yaml's ``asx:`` list is ASX, while US codes must be
    parsed with ``default_exchange=US``).
    """
    text = str(raw or "").strip().upper()
    if not text:
        raise ValueError("empty ticker")

    if ":" in text:
        prefix, rest = text.split(":", 1)
        exchange = _PREFIX_TO_EXCHANGE.get(prefix.strip())
        if exchange:
            return Security(code=rest.strip().rstrip("."), exchange=exchange)
        text = rest.strip()

    if text.endswith("."):          # "RR." is how tickers.yaml writes LSE names
        return Security(code=text.rstrip("."), exchange=LSE)
    if "." in text:
        code, suffix = text.rsplit(".", 1)
        if suffix not in _SUFFIX_TO_EXCHANGE and default_exchange == US and len(suffix) == 1:
            # A US share class ("BRK.B"), not a Yahoo exchange suffix.
            return Security(code=text, exchange=US)
        return Security(code=code, exchange=_SUFFIX_TO_EXCHANGE.get(suffix, suffix))
    return Security(code=text, exchange=default_exchange)


def same_security(a: object, b: object, default_exchange: str = ASX) -> bool:
    """True when *a* and *b* name the same security (``NHC`` vs ``NHC.AX``)."""
    try:
        return parse_security(a, default_exchange).key == parse_security(b, default_exchange).key
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Small helpers with the exact semantics existing call sites already had.
# Each is documented against the behaviour it preserves; prefer
# parse_security() in new code.
# ---------------------------------------------------------------------------

def strip_asx_suffix(ticker: object) -> str:
    """``nhc.ax`` -> ``NHC``; anything without a trailing .AX is just upper-cased.

    Harry's identity rule: portfolio files, Bob and Sally write bare codes,
    Wally writes ``.AX``, and they must match.
    """
    t = str(ticker or "").strip().upper()
    return t[:-3] if t.endswith(".AX") else t


def asx_code_or_none(ticker: object) -> Optional[str]:
    """The bare ASX code of a ``CODE.AX`` ticker, else None (Wally's rule:
    only an explicit ``.AX`` suffix counts as ASX; US/LSE/TSX names have no
    ASX announcements to look up)."""
    t = str(ticker or "").strip().upper()
    if t.endswith(".AX") and len(t) > 3:
        return t[:-3]
    return None


def with_default_suffix(ticker: str, exchange_suffix: str = "AX") -> str:
    """``NHC`` -> ``NHC.AX``; anything that already has a dot is unchanged."""
    if "." in ticker:
        return ticker
    return f"{ticker}.{exchange_suffix}"


def ticker_from_symbol(symbol: object) -> str:
    """``ASX:ABB`` -> ``ABB``, ``LSE:RR.`` -> ``RR``. The exchange prefix and
    the LSE trailing dot are not part of the name (Theo's decision ledger)."""
    text = str(symbol or "").strip().upper()
    if ":" in text:
        text = text.split(":", 1)[1]
    return text.rstrip(".").strip()
