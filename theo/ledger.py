"""Optional transaction ledger.

Everything in Theo works without this module producing anything. If
``data/portfolio.xlsx`` is absent — which it will be until the file is copied
across from the PC — slides render with the judgement and no figures, and the
site shows dashes in the IRR columns. Nothing raises.

When the file *is* present, every BUY is rebuilt as a separate **Decision**.
That is the whole point: three purchases of the same company on three dates at
three prices are three decisions with three different answers, and averaging
them into one line item destroys the only information worth having. A story
told from memory compresses a scaling-in into a single act of conviction; the
ledger does not.

Reconstruction rules
--------------------
* BUY opens a lot.
* DIVIDEND is allocated pro-rata across the lots open on the payment date,
  weighted by shares held.
* SELL consumes lots FIFO; each lot books its own share of the proceeds on
  that date.
* SPLIT scales every open lot.
* Shares still held are valued at the summary table's implied unit price
  (Value AUD / Shares), so the terminal cash flow matches the portfolio's own
  mark rather than a second price source.

IRR is then a plain XIRR over that lot's own cash flows, solved by bisection.
Lots with a cost under $100 carrying more than 100 shares are demerger scrip:
they arrived, they were not bought, and an IRR on a near-zero cost base is a
meaningless number. They are flagged and given none.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import re
from pathlib import Path
from typing import Any, Iterable

DEFAULT_LEDGER_PATH = Path("data/portfolio.xlsx")

# A lot that cost less than this, while carrying more than this many shares,
# did not get bought — it fell out of a demerger.
SCRIP_MAX_COST = 100.0
SCRIP_MIN_SHARES = 100.0

_BLOCK_RE = re.compile(r"^\s*(?:ASX[:\s]+)?([A-Z0-9]{2,6})\s+Transactions\b", re.IGNORECASE)
_NUM_RE = re.compile(r"-?[\d,]*\.?\d+")
_CCY_RE = re.compile(r"\b([A-Z]{3})\b")

TXN_TYPES = ("BUY", "SELL", "DIVIDEND", "SPLIT")


# --------------------------------------------------------------------------
# Cell coercion — spreadsheets are not a data format, they are a rumour
# --------------------------------------------------------------------------


def _num(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    negative = text.startswith("(") and text.endswith(")")
    match = _NUM_RE.search(text.replace("$", ""))
    if not match:
        return None
    try:
        out = float(match.group(0).replace(",", ""))
    except ValueError:
        return None
    return -out if negative else out


def _ccy(value: Any) -> str:
    if value is None:
        return ""
    match = _CCY_RE.search(str(value).upper())
    return match.group(1) if match else ""


def _date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y", "%d %b %Y", "%d %B %Y", "%Y/%m/%d", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


# --------------------------------------------------------------------------
# XIRR
# --------------------------------------------------------------------------


def npv(rate: float, flows: list[tuple[dt.date, float]]) -> float:
    if not flows:
        return 0.0
    start = min(f[0] for f in flows)
    total = 0.0
    for when, amount in flows:
        years = (when - start).days / 365.25
        total += amount / ((1.0 + rate) ** years)
    return total


def xirr(flows: list[tuple[dt.date, float]], lo: float = -0.9999, hi: float = 10.0) -> float | None:
    """Solve NPV(rate) == 0 by bisection. None when there is no sign change.

    Bisection rather than Newton because the function is well behaved on a
    bracketed interval and cannot run away — a wrong IRR that looks plausible
    is worse than no IRR.
    """
    if len(flows) < 2:
        return None
    if not any(a < 0 for _, a in flows) or not any(a > 0 for _, a in flows):
        return None

    f_lo, f_hi = npv(lo, flows), npv(hi, flows)
    if f_lo * f_hi > 0:
        # Widen once before giving up.
        hi = 100.0
        f_hi = npv(hi, flows)
        if f_lo * f_hi > 0:
            return None

    for _ in range(200):
        mid = (lo + hi) / 2.0
        f_mid = npv(mid, flows)
        if abs(f_mid) < 1e-9 or (hi - lo) < 1e-10:
            return mid
        if f_lo * f_mid <= 0:
            hi, f_hi = mid, f_mid
        else:
            lo, f_lo = mid, f_mid
    return (lo + hi) / 2.0


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------


@dataclasses.dataclass
class Transaction:
    ticker: str
    kind: str
    date: dt.date | None
    shares: float | None
    price: float | None
    cash_flow: float | None


@dataclasses.dataclass
class Decision:
    """One BUY, followed to its conclusion."""

    ticker: str
    index: int
    date: dt.date | None
    shares: float = 0.0
    price: float = 0.0
    cost: float = 0.0
    shares_remaining: float = 0.0
    dividends: float = 0.0
    proceeds: float = 0.0
    value_now: float = 0.0
    is_scrip: bool = False
    flows: list[tuple[dt.date, float]] = dataclasses.field(default_factory=list)
    as_at: dt.date | None = None

    @property
    def label(self) -> str:
        return f"{self.ticker} #{self.index}"

    @property
    def total_in(self) -> float:
        return self.dividends + self.proceeds + self.value_now

    @property
    def multiple(self) -> float | None:
        if self.cost <= 0:
            return None
        return self.total_in / self.cost

    @property
    def years(self) -> float | None:
        if not self.date or not self.as_at:
            return None
        return (self.as_at - self.date).days / 365.25

    @property
    def irr(self) -> float | None:
        if self.is_scrip:
            return None
        return xirr(self.flows)

    @property
    def open(self) -> bool:
        return self.shares_remaining > 1e-9


@dataclasses.dataclass
class Holding:
    ticker: str
    shares: float = 0.0
    price: float = 0.0
    value: float = 0.0
    cost_basis: float = 0.0
    unrealized: float = 0.0
    realized: float = 0.0
    dividends: float = 0.0
    currency_gains: float = 0.0
    total_gains: float = 0.0
    currency: str = "AUD"
    summary_irr: float | None = None
    decisions: list[Decision] = dataclasses.field(default_factory=list)

    @property
    def unit_value(self) -> float:
        if self.shares > 0 and self.value:
            return self.value / self.shares
        return self.price or 0.0

    @property
    def irr(self) -> float | None:
        """Capital-weighted IRR across every decision in the name."""
        flows = [f for d in self.decisions if not d.is_scrip for f in d.flows]
        return xirr(sorted(flows, key=lambda f: f[0])) if flows else self.summary_irr

    @property
    def multiple(self) -> float | None:
        cost = sum(d.cost for d in self.decisions if not d.is_scrip)
        if cost <= 0:
            return None
        return sum(d.total_in for d in self.decisions if not d.is_scrip) / cost

    @property
    def years(self) -> float | None:
        spans = [d.years for d in self.decisions if d.years is not None]
        return max(spans) if spans else None


@dataclasses.dataclass
class Ledger:
    holdings: dict[str, Holding] = dataclasses.field(default_factory=dict)
    as_at: dt.date | None = None
    path: Path | None = None
    warnings: list[str] = dataclasses.field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.holdings)

    def get(self, ticker: str) -> Holding | None:
        return self.holdings.get(str(ticker).upper())

    def decisions(self, ticker: str) -> list[Decision]:
        holding = self.get(ticker)
        return holding.decisions if holding else []

    def by_capital(self) -> list[Holding]:
        return sorted(self.holdings.values(), key=lambda h: h.value or 0.0, reverse=True)

    def tickers(self) -> list[str]:
        return sorted(self.holdings)


# The empty ledger. Callers never branch on None.
EMPTY = Ledger()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def _rows_from_sheet(path: Path) -> list[list[Any]] | None:
    try:
        import openpyxl  # noqa: PLC0415 — optional dependency, by design
    except ImportError:
        return None
    workbook = openpyxl.load_workbook(path, data_only=True, read_only=True)
    sheet = None
    for name in workbook.sheetnames:
        if "transaction" in name.lower():
            sheet = workbook[name]
            break
    if sheet is None:
        sheet = workbook[workbook.sheetnames[0]]
    return [list(r) for r in sheet.iter_rows(values_only=True)]


def _looks_like_header(row: list[Any]) -> bool:
    cells = {_str(c).lower() for c in row if c is not None}
    return "type" in cells and "date" in cells


def _parse_summary(rows: list[list[Any]]) -> tuple[dict[str, Holding], int]:
    """Read the summary table at the top. Returns (holdings, first row after)."""
    holdings: dict[str, Holding] = {}
    header_idx = None
    columns: dict[str, int] = {}

    for i, row in enumerate(rows[:60]):
        labels = [_str(c).lower() for c in row]
        if "ticker" in labels and any("value" in c for c in labels):
            header_idx = i
            for j, label in enumerate(labels):
                if not label:
                    continue
                if label == "ticker":
                    columns["ticker"] = j
                elif label == "shares":
                    columns["shares"] = j
                elif label == "price":
                    columns["price"] = j
                elif "value" in label:
                    columns["value"] = j
                elif "cost" in label:
                    columns["cost"] = j
                elif "unrealiz" in label or "unrealis" in label:
                    columns["unrealized"] = j
                elif "realiz" in label or "realis" in label:
                    columns["realized"] = j
                elif "dividend" in label:
                    columns["dividends"] = j
                elif "currency" in label:
                    columns["currency_gains"] = j
                elif "total gain" in label:
                    columns["total_gains"] = j
                elif "irr" in label:
                    columns["irr"] = j
            break

    if header_idx is None:
        return holdings, 0

    last = header_idx
    for i in range(header_idx + 1, len(rows)):
        row = rows[i]
        raw_ticker = _str(row[columns["ticker"]]) if columns.get("ticker") is not None else ""
        if not raw_ticker:
            if any(_str(c) for c in row):
                continue
            break
        if _BLOCK_RE.match(raw_ticker):
            break
        ticker = raw_ticker.split(":")[-1].strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2,6}", ticker):
            continue

        def cell(key: str) -> Any:
            idx = columns.get(key)
            return row[idx] if idx is not None and idx < len(row) else None

        irr_raw = _num(cell("irr"))
        if irr_raw is not None and abs(irr_raw) > 1.5:
            irr_raw = irr_raw / 100.0
        holdings[ticker] = Holding(
            ticker=ticker,
            shares=_num(cell("shares")) or 0.0,
            price=_num(cell("price")) or 0.0,
            value=_num(cell("value")) or 0.0,
            cost_basis=_num(cell("cost")) or 0.0,
            unrealized=_num(cell("unrealized")) or 0.0,
            realized=_num(cell("realized")) or 0.0,
            dividends=_num(cell("dividends")) or 0.0,
            currency_gains=_num(cell("currency_gains")) or 0.0,
            total_gains=_num(cell("total_gains")) or 0.0,
            currency=_ccy(cell("price")) or "AUD",
            summary_irr=irr_raw,
        )
        last = i
    return holdings, last + 1


def _parse_blocks(rows: list[list[Any]], start: int) -> dict[str, list[Transaction]]:
    """Read the per-ticker transaction blocks below the summary."""
    blocks: dict[str, list[Transaction]] = {}
    ticker: str | None = None
    columns: dict[str, int] = {}

    for row in rows[start:]:
        first = ""
        for cell in row:
            if _str(cell):
                first = _str(cell)
                break

        match = _BLOCK_RE.match(first)
        if match:
            ticker = match.group(1).upper()
            blocks.setdefault(ticker, [])
            columns = {}
            continue

        if ticker is None:
            continue

        if _looks_like_header(row):
            columns = {}
            for j, cell in enumerate(row):
                label = _str(cell).lower()
                if not label:
                    continue
                if label.startswith("type"):
                    columns["type"] = j
                elif label.startswith("date"):
                    columns["date"] = j
                elif label.startswith("share") or label.startswith("unit") or label.startswith("qty"):
                    columns["shares"] = j
                elif label.startswith("price"):
                    columns["price"] = j
                elif "cash" in label or "amount" in label or "value" in label:
                    columns["cash"] = j
            continue

        if not columns:
            continue

        kind = _str(row[columns["type"]]).upper() if columns.get("type") is not None else ""
        if kind not in TXN_TYPES:
            continue

        def cell(key: str) -> Any:
            idx = columns.get(key)
            return row[idx] if idx is not None and idx < len(row) else None

        blocks[ticker].append(
            Transaction(
                ticker=ticker,
                kind=kind,
                date=_date(cell("date")),
                shares=_num(cell("shares")),
                price=_num(cell("price")),
                cash_flow=_num(cell("cash")),
            )
        )

    return blocks


# --------------------------------------------------------------------------
# Decision reconstruction
# --------------------------------------------------------------------------


def _build_decisions(
    ticker: str,
    transactions: list[Transaction],
    unit_value: float,
    as_at: dt.date,
    warnings: list[str],
) -> list[Decision]:
    ordered = sorted(
        [t for t in transactions if t.date],
        key=lambda t: (t.date, TXN_TYPES.index(t.kind) if t.kind in TXN_TYPES else 9),
    )
    lots: list[Decision] = []

    for txn in ordered:
        if txn.kind == "BUY":
            shares = txn.shares or 0.0
            price = txn.price
            cost = abs(txn.cash_flow) if txn.cash_flow else None
            if cost is None:
                cost = shares * (price or 0.0)
            if price is None and shares:
                price = cost / shares
            lot = Decision(
                ticker=ticker,
                index=len(lots) + 1,
                date=txn.date,
                shares=shares,
                price=price or 0.0,
                cost=cost or 0.0,
                shares_remaining=shares,
                as_at=as_at,
            )
            lot.is_scrip = lot.cost < SCRIP_MAX_COST and lot.shares > SCRIP_MIN_SHARES
            lot.flows.append((txn.date, -lot.cost))
            lots.append(lot)

        elif txn.kind == "DIVIDEND":
            amount = txn.cash_flow
            if amount is None and txn.shares and txn.price:
                amount = txn.shares * txn.price
            if not amount:
                continue
            amount = abs(amount)
            open_lots = [lot for lot in lots if lot.shares_remaining > 1e-9]
            base = sum(lot.shares_remaining for lot in open_lots)
            if base <= 0:
                continue
            for lot in open_lots:
                share = amount * (lot.shares_remaining / base)
                lot.dividends += share
                lot.flows.append((txn.date, share))

        elif txn.kind == "SELL":
            to_sell = abs(txn.shares or 0.0)
            price = txn.price
            if price is None and txn.cash_flow and to_sell:
                price = abs(txn.cash_flow) / to_sell
            price = price or 0.0
            for lot in lots:
                if to_sell <= 1e-9:
                    break
                if lot.shares_remaining <= 1e-9:
                    continue
                taken = min(lot.shares_remaining, to_sell)
                lot.shares_remaining -= taken
                to_sell -= taken
                cash = taken * price
                lot.proceeds += cash
                lot.flows.append((txn.date, cash))
            if to_sell > 1e-6:
                warnings.append(
                    f"{ticker}: sell on {txn.date} exceeded open lots by {to_sell:g} shares"
                )

        elif txn.kind == "SPLIT":
            open_lots = [lot for lot in lots if lot.shares_remaining > 1e-9]
            before = sum(lot.shares_remaining for lot in open_lots)
            raw = txn.shares if txn.shares is not None else txn.price
            if not raw or before <= 0:
                continue
            # The sheet writes either the post-split total or a bare ratio.
            ratio = (raw / before) if raw > before else raw
            if ratio <= 0:
                continue
            for lot in open_lots:
                lot.shares_remaining *= ratio
                lot.shares *= ratio
                if lot.shares:
                    lot.price = lot.cost / lot.shares

    for lot in lots:
        if lot.shares_remaining > 1e-9:
            lot.value_now = lot.shares_remaining * unit_value
            if lot.value_now:
                lot.flows.append((as_at, lot.value_now))
        lot.flows.sort(key=lambda f: f[0])

    return lots


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def available(path: str | Path | None = None) -> bool:
    return Path(path or DEFAULT_LEDGER_PATH).is_file()


def load(path: str | Path | None = None, as_at: dt.date | None = None) -> Ledger:
    """Load the ledger. Returns an empty Ledger when the file is not there.

    Never raises for a missing file or a missing openpyxl — the ledger is
    optional and the rest of Theo is expected to run without it.
    """
    target = Path(path or DEFAULT_LEDGER_PATH)
    ledger = Ledger(path=target, as_at=as_at or dt.date.today())
    if not target.is_file():
        return ledger

    rows = _rows_from_sheet(target)
    if rows is None:
        ledger.warnings.append(
            "openpyxl is not installed — ledger ignored (pip install openpyxl)"
        )
        return ledger

    holdings, next_row = _parse_summary(rows)
    blocks = _parse_blocks(rows, next_row)

    for ticker, transactions in blocks.items():
        holding = holdings.get(ticker)
        if holding is None:
            holding = Holding(ticker=ticker)
            holdings[ticker] = holding
        holding.decisions = _build_decisions(
            ticker, transactions, holding.unit_value, ledger.as_at, ledger.warnings
        )

    ledger.holdings = holdings
    return ledger


def gaps(ledger: Ledger, covered: Iterable[str]) -> list[Holding]:
    """Holdings with real capital in them and no thesis written, by size."""
    have = {str(t).upper() for t in covered}
    missing = [h for t, h in ledger.holdings.items() if t not in have and (h.shares or 0) > 0]
    return sorted(missing, key=lambda h: h.value or 0.0, reverse=True)
