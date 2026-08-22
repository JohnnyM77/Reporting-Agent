"""Theo — thesis accountability manager.

Theo records why a holding was bought, what would make it a sell, and flags
when the reasoning has drifted away from the reasoning that was written down.

Core principle: thesis files hold judgement only, never numbers. Every figure
on a slide comes from the transaction ledger at render time, so a slide cannot
go stale and a typo cannot be baked into a permanent record.
"""

__all__ = ["thesis", "ledger", "drift", "season", "render", "publish", "cli"]

__version__ = "1.0.0"
