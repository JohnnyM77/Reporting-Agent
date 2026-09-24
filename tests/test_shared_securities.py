"""shared/securities.py: one answer to "are these the same security?"."""

from __future__ import annotations

import pytest

from shared.securities import (
    ASX, LSE, SGX, US, Security, asx_code_or_none, parse_security, same_security,
    strip_asx_suffix, ticker_from_symbol, with_default_suffix,
)


@pytest.mark.parametrize("raw,key,yahoo", [
    ("NHC", "ASX:NHC", "NHC.AX"),
    ("nhc.ax", "ASX:NHC", "NHC.AX"),
    ("ASX:NHC", "ASX:NHC", "NHC.AX"),
    (" NHC.AX ", "ASX:NHC", "NHC.AX"),
    ("RR.", "LSE:RR", "RR.L"),
    ("RR.L", "LSE:RR", "RR.L"),
    ("LSE:RR.", "LSE:RR", "RR.L"),
    ("D05.SI", "SGX:D05", "D05.SI"),
    ("SGX:D05", "SGX:D05", "D05.SI"),
    ("ATM.NZ", "NZ:ATM", "ATM.NZ"),
    ("2914.T", "T:2914", "2914.T"),
])
def test_parse(raw, key, yahoo):
    sec = parse_security(raw)
    assert sec.key == key and sec.yahoo == yahoo


def test_bare_code_takes_default_exchange():
    assert parse_security("POOL", default_exchange=US) == Security("POOL", US)
    assert parse_security("POOL", default_exchange=US).yahoo == "POOL"
    assert parse_security("D05", default_exchange=SGX).yahoo == "D05.SI"
    assert parse_security("BRK.B", default_exchange=US) == Security("BRK.B", US)


def test_same_security():
    assert same_security("NHC", "NHC.AX")
    assert same_security("ASX:ABB", "abb")
    assert same_security("RR.", "RR.L")
    assert not same_security("NHC", "NHC.L")
    assert not same_security("", "NHC")


def test_units():
    assert parse_security("RR.").quote_currency == "GBX"
    assert parse_security("NHC").asx_code == "NHC" and parse_security("RR.").asx_code is None


def test_parse_rejects_empty():
    with pytest.raises(ValueError):
        parse_security("  ")


# Helpers that pin the exact semantics of the call sites they replaced.

def test_strip_asx_suffix_matches_harry_bare():
    assert [strip_asx_suffix(t) for t in ("nhc.ax", "NHC", "RR.", None)] == ["NHC", "NHC", "RR.", ""]


def test_asx_code_or_none_matches_wally():
    assert asx_code_or_none("bhp.ax") == "BHP"
    assert asx_code_or_none(".AX") is None
    assert asx_code_or_none("POOL") is None and asx_code_or_none("RR.L") is None and asx_code_or_none(None) is None


def test_with_default_suffix_matches_master_engine():
    assert with_default_suffix("NHC") == "NHC.AX"
    assert with_default_suffix("RR.L") == "RR.L" and with_default_suffix("POOL", "US") == "POOL.US"


def test_ticker_from_symbol_matches_theo_ledger():
    assert ticker_from_symbol("ASX:ABB") == "ABB"
    assert ticker_from_symbol("LSE:RR.") == "RR"
    assert ticker_from_symbol(" nhc ") == "NHC" and ticker_from_symbol(None) == ""
