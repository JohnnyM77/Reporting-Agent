"""Config loading for Captain Hindsight.

Thresholds and rules come from ``config/hindsight.yaml``; a handful of env
vars override the bits that change per run (store, models, caps). Nothing
personal is read from here.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "config" / "hindsight.yaml"
FRAMEWORKS_DIR = Path(__file__).resolve().parent / "frameworks"

MODEL_DEFAULT_FULL = "claude-sonnet-4-6"  # same default agent.py uses


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(path: str | Path | None = None, overrides: dict | None = None) -> dict[str, Any]:
    target = Path(path) if path else Path(os.environ.get("HINDSIGHT_CONFIG", CONFIG_PATH))
    data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    if overrides:
        data = _deep_merge(data, overrides)

    llm = data.setdefault("llm", {})
    env = os.environ
    llm["model_triage"] = env.get("HINDSIGHT_MODEL_TRIAGE") or llm.get("model_triage") or "claude-haiku-4-5"
    llm["model_full"] = (
        env.get("HINDSIGHT_MODEL_FULL")
        or llm.get("model_full")
        or env.get("CLAUDE_MODEL")
        or MODEL_DEFAULT_FULL
    )
    if env.get("HINDSIGHT_MAX_LLM_CALLS"):
        llm["max_calls_per_run"] = int(env["HINDSIGHT_MAX_LLM_CALLS"])
    if env.get("HINDSIGHT_MAX_TOKENS_FULL"):
        llm["max_tokens_full"] = int(env["HINDSIGHT_MAX_TOKENS_FULL"])

    store = data.setdefault("store", {})
    if env.get("HINDSIGHT_STORE"):
        store["backend"] = env["HINDSIGHT_STORE"].strip()
    return data


def load_framework(name: str) -> dict[str, Any]:
    return yaml.safe_load((FRAMEWORKS_DIR / name).read_text(encoding="utf-8")) or {}


def seven_powers() -> dict[str, Any]:
    return load_framework("seven_powers.yaml")


def munger_tendencies() -> dict[str, Any]:
    return load_framework("munger_tendencies.yaml")
