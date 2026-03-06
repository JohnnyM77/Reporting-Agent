from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import matplotlib.pyplot as plt
import pandas as pd
import yaml

from .data_fetch import fetch_price_history_10y_monthly
from .screening import TickerScreenResult


@dataclass
class ValuationConfig:
    ticker: str
    method: str
    multiple: float
    required_return_dividend: Optional[float]
    eps: dict[int, float]
    dividend: dict[int, float]


def _load_valuation_config(ticker: str) -> Optional[ValuationConfig]:
    cfg_path = Path("valuations") / f"{ticker.lower().replace('.', '_')}.yaml"
    if not cfg_path.exists():
        return None

    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    series = data.get("series", {})
    eps = {int(y): float(v) for y, v in (series.get("eps", {}) or {}).items()}
    dividend = {int(y): float(v) for y, v in (series.get("dividend", {}) or {}).items()}
    return ValuationConfig(
        ticker=str(data.get("ticker") or ticker),
        method=str(data.get("method") or "earnings_multiple"),
        multiple=float(data.get("multiple") or 0),
        required_return_dividend=(
            float(data["required_return_dividend"]) if data.get("required_return_dividend") is not None else None
        ),
        eps=eps,
        dividend=dividend,
    )


def render_range_chart(result: TickerScreenResult, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{result.ticker.lower().replace('.', '_')}_range.png"

    fig, ax = plt.subplots(figsize=(5.2, 1.2))
    ax.hlines(0, result.low_52w, result.high_52w, linewidth=8, color="#d1d5db")
    ax.scatter([result.low_52w, result.current_price, result.high_52w], [0, 0, 0], c=["#374151", "#dc2626", "#374151"], s=40)
    ax.text(result.low_52w, 0.12, f"Low {result.low_52w:.2f}", fontsize=8, ha="left")
    ax.text(result.current_price, -0.18, f"Now {result.current_price:.2f}", fontsize=8, ha="center")
    ax.text(result.high_52w, 0.12, f"High {result.high_52w:.2f}", fontsize=8, ha="right")
    ax.set_yticks([])
    ax.set_title(f"{result.ticker} — 52-week range", fontsize=9)
    ax.spines[["left", "right", "top"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)
    return out


def render_value_vs_price_chart(ticker: str, out_dir: Path) -> tuple[Optional[Path], str]:
    valuation = _load_valuation_config(ticker)
    if not valuation:
        return None, "No valuation config found yet for this ticker"

    price = fetch_price_history_10y_monthly(ticker)
    if price.empty:
        return None, "No 10-year price history available"

    years = sorted(set(valuation.eps.keys()) | set(valuation.dividend.keys()))
    if not years:
        return None, "Valuation config exists but no EPS/dividend series provided"

    idx = pd.to_datetime([f"{y}-12-31" for y in years])
    value_1 = pd.Series([valuation.eps.get(y, float("nan")) * valuation.multiple for y in years], index=idx)

    value_2 = None
    if valuation.required_return_dividend and valuation.required_return_dividend > 0:
        value_2 = pd.Series(
            [valuation.dividend.get(y, float("nan")) / valuation.required_return_dividend for y in years],
            index=idx,
        )

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"{ticker.lower().replace('.', '_')}_value.png"

    fig, ax = plt.subplots(figsize=(7, 3.8))
    ax.plot(price.index, price.values, color="#111827", linewidth=1.8, label="Price")
    ax.step(value_1.index, value_1.values, where="post", color="#2563eb", linewidth=1.7, label="Value (EPS × multiple)")
    if value_2 is not None:
        ax.step(value_2.index, value_2.values, where="post", color="#059669", linewidth=1.5, label="Value (Dividend / RRoR)")

    ax.set_title(f"{ticker} — 10Y Price vs Value")
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best", fontsize=8)
    fig.tight_layout()
    fig.savefig(out, dpi=150)
    plt.close(fig)

    return out, "ok"
