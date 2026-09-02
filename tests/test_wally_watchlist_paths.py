"""Wally's configured watchlist paths must point at the files people edit.

The bug this pins: STANDARD_WATCHLISTS pointed at .github/Watchlist/, an
older copy, while the buy-price sync and the TII -> JM -> Aussie Tech
de-duplication were written to watchlists/. Wally kept screening the
March-2026 TII list, every row came back target_price=null, and the entire
below-target trigger was dead — with no error anywhere. A run that screens
the wrong file looks exactly like a run that screens the right one, so the
paths and the prices both need asserting.
"""

import yaml
from pathlib import Path

from wally.config import STANDARD_WATCHLISTS, TII75_WATCHLIST
from wally.watchlist_loader import load_watchlist

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_every_configured_watchlist_file_exists():
    for rel in [*STANDARD_WATCHLISTS, TII75_WATCHLIST]:
        assert (REPO_ROOT / rel).is_file(), f"configured watchlist missing: {rel}"


def test_configured_watchlists_are_the_ones_under_watchlists_dir():
    """Guards against a second copy drifting back into existence."""
    for rel in [*STANDARD_WATCHLISTS, TII75_WATCHLIST]:
        assert rel.startswith("watchlists/"), (
            f"{rel} is outside watchlists/ — buy prices and de-duplication are "
            "maintained there, so screening anything else silently loses them"
        )


def test_tii_watchlist_actually_carries_buy_prices():
    """The below-target trigger is only alive if targets reach the loader."""
    tii = next(p for p in STANDARD_WATCHLISTS if "tii_watchlist" in p)
    wl = load_watchlist(REPO_ROOT / tii)
    assert len(wl.target_prices) > 50, (
        f"TII watchlist loaded only {len(wl.target_prices)} target prices — "
        "the below-target screen is effectively off"
    )
    # Prices must key onto tickers the screen will actually look up.
    tickers = set(wl.tickers)
    orphans = sorted(set(wl.target_prices) - tickers)
    assert not orphans, f"target prices for tickers not in the list: {orphans}"


def test_target_prices_are_positive_numbers():
    tii = next(p for p in STANDARD_WATCHLISTS if "tii_watchlist" in p)
    wl = load_watchlist(REPO_ROOT / tii)
    for ticker, price in wl.target_prices.items():
        assert isinstance(price, float) and price > 0, f"{ticker}: {price!r}"


def test_no_stale_duplicate_watchlist_directory():
    """.github/Watchlist/ was the stale copy; it must not come back."""
    assert not (REPO_ROOT / ".github" / "Watchlist").exists(), (
        "a second watchlist directory has reappeared — one of the two copies "
        "will go stale and Wally will silently screen the wrong one"
    )


def test_pence_buy_prices_are_parsed_to_numbers():
    """UK names carry GBX prices as '500.00p' in the source spreadsheet."""
    tii = next(p for p in STANDARD_WATCHLISTS if "tii_watchlist" in p)
    raw = yaml.safe_load((REPO_ROOT / tii).read_text(encoding="utf-8"))
    pence = {k: v for k, v in (raw.get("targets") or {}).items()
             if isinstance(v, str) and v.strip().endswith("p")}
    if not pence:
        return  # none in the list right now; nothing to assert
    wl = load_watchlist(REPO_ROOT / tii)
    for ticker in pence:
        key = ticker.strip().upper()
        assert isinstance(wl.target_prices.get(key), float), (
            f"{ticker} pence price {pence[ticker]!r} did not parse to a number"
        )
