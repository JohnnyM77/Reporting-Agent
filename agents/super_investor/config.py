# agents/super_investor/config.py
#
# Configuration, thresholds, and scoring weights for the Super Investor Agent.
# Values can be overridden via config/priorities.yaml at runtime.

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Default scoring weights — Base event severity
# ---------------------------------------------------------------------------
BASE_SEVERITY: dict[str, int] = {
    "earnings_release": 50,
    "guidance_change": 50,
    "capital_raise": 50,
    "takeover": 50,
    "regulator_action": 50,
    "major_contract": 30,
    "ceo_change": 30,
    "litigation": 30,
    "profit_warning": 30,
    "appendix_4d": 50,
    "appendix_4e": 50,
    "acquisition": 50,
    "valuation_trigger": 20,
    "near_52w_low": 10,
    "generic_news": 5,
}

# ---------------------------------------------------------------------------
# Universe relevance bonus
# ---------------------------------------------------------------------------
UNIVERSE_BONUS: dict[str, int] = {
    "portfolio": 20,
    "high_conviction": 15,
    "TII75": 10,
    "other": 0,
}

# ---------------------------------------------------------------------------
# Valuation / opportunity bonus
# ---------------------------------------------------------------------------
VALUATION_BONUS_WITHIN_5PCT_LOW: int = 15
VALUATION_BONUS_WITHIN_2PCT_LOW: int = 25
VALUATION_BONUS_REVERSE_DCF_ATTRACTIVE: int = 25
VALUATION_BONUS_BELOW_TARGET_BUY_BAND: int = 20

# ---------------------------------------------------------------------------
# Recency bonus
# ---------------------------------------------------------------------------
RECENCY_BONUS_WITHIN_2H: int = 10
RECENCY_BONUS_SAME_DAY: int = 5

# ---------------------------------------------------------------------------
# Priority band thresholds
# ---------------------------------------------------------------------------
THRESHOLD_CRITICAL: int = 80
THRESHOLD_HIGH: int = 55
THRESHOLD_MEDIUM: int = 30
THRESHOLD_LOW: int = 10

# ---------------------------------------------------------------------------
# Future-ready hooks — placeholder weight registry
# ---------------------------------------------------------------------------
# These will be used once full implementations are available.
FUTURE_HOOKS: dict[str, Any] = {
    "insider_trading": {"weight": 0, "enabled": False},
    "broker_target_change": {"weight": 0, "enabled": False},
    "short_interest": {"weight": 0, "enabled": False},
    "reverse_dcf_engine": {"weight": 0, "enabled": False},
    "conviction_scoring": {"weight": 0, "enabled": False},
}

# ---------------------------------------------------------------------------
# Config file loader
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_PRIORITIES_YAML = _REPO_ROOT / "config" / "priorities.yaml"


def load_priorities_yaml() -> dict[str, Any]:
    """
    Load config/priorities.yaml if it exists and update the module-level
    defaults.  Returns the raw YAML dict (or an empty dict on failure).
    """
    if not _PRIORITIES_YAML.exists():
        return {}
    try:
        import yaml
        with open(_PRIORITIES_YAML) as fh:
            data = yaml.safe_load(fh) or {}
        logger.debug("[super_investor/config] Loaded %s", _PRIORITIES_YAML)
        return data
    except Exception as exc:
        logger.warning(
            "[super_investor/config] Could not load priorities.yaml: %s", exc
        )
        return {}
