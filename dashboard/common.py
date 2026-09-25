"""Helpers every dashboard section may use: HTML escaping, date formatting,
badges, percentage bars, the inlined favicon."""

from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Favicon
# ---------------------------------------------------------------------------

FAVICON_PATH = REPO_ROOT / "assets" / "favicon.png"

# The emoji SVG that was here before, kept as the fallback so a missing or
# unreadable asset degrades to the old icon instead of breaking the build.
_FAVICON_FALLBACK = (
    "data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'>"
    "<text y='.9em' font-size='90'>&#129302;</text></svg>"
)


def favicon_data_uri(path: Path = FAVICON_PATH) -> str:
    """Inline the favicon so every page stays a single self-contained file.

    Theo's site is deliberately standalone — it has to work from a file:// URL
    with no sibling assets — so an external <link href="favicon.png"> is not an
    option there. Embedding costs ~14KB of base64 and keeps that guarantee.
    """
    try:
        return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()
    except OSError:
        return _FAVICON_FALLBACK

def _fmt_date(iso: str | None) -> str:
    """Format an ISO timestamp as ``4 Aug 2026`` (no leading zero on day).

    Uses ``d.day`` + strftime for the rest so it works on Windows too.
    ``%-d`` is a POSIX extension; on Windows strftime raises
    ``ValueError: Invalid format string`` and the whole dashboard build
    dies, taking Slinger's publish step down with it -- Bob's ubuntu
    runner never trips this, Slinger's self-hosted Windows runner did."""
    if not iso:
        return "Never"
    try:
        d = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return f"{d.day} {d.strftime('%b %Y')}"
    except Exception:
        return iso


def _pct_bar(pct: float, max_pct: float = 10.0, good_direction: str = "low") -> str:
    """Mini HTML bar showing how far a stock is from its low/high."""
    clamped = min(max(pct, 0), max_pct)
    width = int((clamped / max_pct) * 100)
    colour = "#22c55e" if good_direction == "low" and pct < 3 else "#f59e0b" if pct < 7 else "#ef4444"
    return (
        f"<div style='background:#1e293b;border-radius:3px;height:8px;width:80px;display:inline-block;vertical-align:middle'>"
        f"<div style='background:{colour};height:8px;border-radius:3px;width:{width}%'></div></div>"
    )


def _tier_badge(tier: str) -> str:
    colours = {
        "Tier 1: Watch":        "#3b82f6",
        "Tier 2: Review":       "#f59e0b",
        "Tier 3: Deep Review":  "#ef4444",
    }
    bg = colours.get(tier, "#64748b")
    short = tier.replace("Tier 1: ", "T1 ").replace("Tier 2: ", "T2 ").replace("Tier 3: ", "T3 ")
    return f"<span style='background:{bg};color:#fff;padding:2px 6px;border-radius:4px;font-size:11px'>{short}</span>"

def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _section_cell(label: str, value) -> str:
    if not value:
        return ""
    if isinstance(value, list):
        items = "".join(f"<li style='margin:2px 0'>{_esc(str(v))}</li>" for v in value if v)
        body = f"<ul style='margin:4px 0 0 14px;padding:0'>{items}</ul>"
    elif isinstance(value, dict):
        row_parts = []
        for k, v in value.items():
            if not v:
                continue
            label = _esc(k.replace("_", " ").title())
            if isinstance(v, list):
                items_html = "".join(f"<li style='margin:1px 0'>{_esc(str(i))}</li>" for i in v if i)
                row_parts.append(
                    f"<div style='margin:2px 0'><span style='color:#64748b'>{label}:</span>"
                    f"<ul style='margin:2px 0 0 14px;padding:0'>{items_html}</ul></div>"
                )
            else:
                row_parts.append(
                    f"<div style='margin:2px 0'><span style='color:#64748b'>{label}:</span> {_esc(str(v))}</div>"
                )
        body = f"<div style='margin-top:4px'>{''.join(row_parts)}</div>"
    else:
        body = f"<div style='margin-top:4px'>{_esc(str(value))}</div>"
    return (
        f"<div style='background:#0f172a;border-radius:6px;padding:10px 12px;min-width:0'>"
        f"<div style='color:#64748b;font-size:10px;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:2px'>{_esc(label)}</div>"
        f"<div style='color:#e2e8f0;font-size:12px;line-height:1.5'>{body}</div>"
        f"</div>"
    )
