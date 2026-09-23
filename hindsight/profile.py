"""Johnny's bias profile: places to look, not conclusions.

Lives only in the private store (``profile/jm_bias_profile.yaml``). Seeded
from the generic tendency names in ``config/hindsight.yaml`` as KNOWN
VULNERABILITY. The autopsy loop updates each tendency's hit rate; tendencies
that keep producing useless flags are deprioritised automatically.
"""

from __future__ import annotations

import yaml

from .store import Store

REL = "profile/jm_bias_profile.yaml"
HEADER = (
    "# Places to look, not conclusions. A checklist for asking better questions,\n"
    "# not a psychological diagnosis. Updated by Captain Hindsight's autopsy loop.\n"
)


def load(store: Store, cfg: dict) -> dict:
    path = store.root / REL
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    else:
        data = {}
    tend = data.setdefault("tendencies", {})
    for name in cfg.get("bias_profile", {}).get("seed", []):
        tend.setdefault(name, {"state": "KNOWN VULNERABILITY", "flags": 0, "useful": 0, "deprioritised": False})
    return data


def save(store: Store, data: dict) -> None:
    store.write_text(REL, HEADER + yaml.safe_dump(data, sort_keys=True))


def focus_list(profile: dict) -> list[str]:
    return [n for n, t in profile.get("tendencies", {}).items()
            if t.get("state") == "KNOWN VULNERABILITY" and not t.get("deprioritised")]


def record_flags(profile: dict, tendencies: list[str]) -> None:
    for name in tendencies:
        t = profile["tendencies"].setdefault(name, {"state": "UNKNOWN", "flags": 0, "useful": 0, "deprioritised": False})
        t["flags"] = int(t.get("flags", 0)) + 1


def record_usefulness(profile: dict, tendencies: list[str], useful: bool, cfg: dict) -> None:
    bcfg = cfg.get("bias_profile", {})
    min_flags = int(bcfg.get("deprioritise_min_flags", 5))
    max_rate = float(bcfg.get("deprioritise_max_useful_rate", 0.2))
    for name in tendencies:
        t = profile["tendencies"].setdefault(name, {"state": "UNKNOWN", "flags": 1, "useful": 0, "deprioritised": False})
        t["autopsied"] = int(t.get("autopsied", 0)) + 1
        if useful:
            t["useful"] = int(t.get("useful", 0)) + 1
        judged = t["autopsied"]
        rate = t["useful"] / judged if judged else 0.0
        t["useful_rate"] = round(rate, 2)
        t["deprioritised"] = judged >= min_flags and rate < max_rate
