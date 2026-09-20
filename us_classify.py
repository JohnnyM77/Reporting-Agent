"""
us_classify.py -- SEC-form-based classifier for the US Bob agent.

Mirrors sgx_classify.py's shape (input dict -> bucket string) but the
signal is much stronger on EDGAR: every filing carries a canonical
`form` (10-K, 10-Q, 8-K, S-1, ...) and 8-Ks carry a comma-separated
`items` field indicating which trigger items were disclosed. We key
off those directly rather than title-regex, so nothing here has to
guess at wording the way ASX/SGX classification sometimes does.

Output buckets stay in line with the ASX/SGX classifiers so downstream
switch/case can be shared.
"""

from __future__ import annotations

from typing import Dict


# --- direct form-to-bucket mappings ----------------------------------------
_FORM_TO_BUCKET: Dict[str, str] = {
    # Periodic financial reports.
    "10-K":  "RESULTS_HY_FY",
    "10-K/A": "RESULTS_HY_FY",   # /A = amendment; still results content
    "10-Q":  "RESULTS_HY_FY",
    "10-Q/A": "RESULTS_HY_FY",
    "20-F":  "RESULTS_HY_FY",    # foreign private issuer annual (e.g. Sea Ltd)
    "20-F/A": "RESULTS_HY_FY",
    "40-F":  "RESULTS_HY_FY",    # Canadian FPI annual
    "6-K":   "OTHER",            # FPI interim -- can be results OR misc; refine below
    # Capital raises / offerings.
    "S-1":   "CAPITAL_OR_DEBT_RAISE",
    "S-1/A": "CAPITAL_OR_DEBT_RAISE",
    "S-3":   "CAPITAL_OR_DEBT_RAISE",
    "S-3/A": "CAPITAL_OR_DEBT_RAISE",
    "424B1": "CAPITAL_OR_DEBT_RAISE",
    "424B2": "CAPITAL_OR_DEBT_RAISE",
    "424B3": "CAPITAL_OR_DEBT_RAISE",
    "424B4": "CAPITAL_OR_DEBT_RAISE",
    "424B5": "CAPITAL_OR_DEBT_RAISE",
    "F-1":   "CAPITAL_OR_DEBT_RAISE",
    "F-3":   "CAPITAL_OR_DEBT_RAISE",
    # Merger/tender-offer disclosures.
    "SC 13D": "OTHER",
    "SC 13G": "OTHER",
    "SC 14D9": "ACQUISITION",
    "SC TO-T": "ACQUISITION",
}

# 8-K item numbers -> bucket. An 8-K can list multiple items; we take
# the highest-priority match (results > acquisition > capital > other).
_ITEM_TO_BUCKET: Dict[str, str] = {
    "2.01": "ACQUISITION",         # completion of acquisition or disposition of assets
    "2.02": "RESULTS_HY_FY",       # results of operations and financial condition
    "1.01": "OTHER",               # material definitive agreement -- often M&A but too broad
    "3.02": "CAPITAL_OR_DEBT_RAISE",  # unregistered equity sales
    "3.03": "CAPITAL_OR_DEBT_RAISE",  # material modification to rights of security holders
    "7.01": "OTHER",               # Reg FD disclosure (catch-all press release)
    "8.01": "OTHER",               # other events
}

# Priority order when an 8-K lists multiple items -- earlier wins.
_ITEM_PRIORITY = ["RESULTS_HY_FY", "ACQUISITION", "CAPITAL_OR_DEBT_RAISE", "OTHER"]


def _period_type_from_form(form: str) -> str:
    """Coarse period_type guess from the SEC form. The LLM itself reads
    the filing and refines this (Q3 vs FY etc.), but the caller wants
    something reasonable to seed the prompt with."""
    form = (form or "").upper()
    if form.startswith("10-K") or form.startswith("20-F") or form.startswith("40-F"):
        return "full_year"
    if form.startswith("10-Q"):
        return "quarterly"
    return "other"


def period_type_hint(item: Dict) -> str:
    """Public helper -- best-guess period_type for the LLM prompt."""
    return _period_type_from_form(item.get("form") or "")


def classify_us_filing(item: Dict) -> str:
    """Return the Bob-shaped classification bucket for one EDGAR filing
    dict (as produced by us_fetch._normalize_row). Consults, in order:

      1. Direct form-to-bucket mapping (covers 10-K/10-Q, S-1, etc.).
      2. 8-K item numbers (2.02 -> RESULTS, 2.01 -> ACQUISITION, ...).
      3. Falls back to "OTHER" -- unclassified means FYI stream.

    Never returns None."""
    form = (item.get("form") or "").strip().upper()
    if not form:
        return "OTHER"

    # 8-K needs item inspection -- do that first even though 8-K is
    # not in _FORM_TO_BUCKET, so we catch results 8-Ks that pair with a
    # 10-Q on the same reporting period.
    if form == "8-K" or form == "8-K/A":
        items = [x.strip() for x in (item.get("items") or "").split(",") if x.strip()]
        buckets = [_ITEM_TO_BUCKET.get(it, "OTHER") for it in items]
        if not buckets:
            return "OTHER"
        for candidate in _ITEM_PRIORITY:
            if candidate in buckets:
                return candidate
        return "OTHER"

    bucket = _FORM_TO_BUCKET.get(form)
    if bucket:
        return bucket
    return "OTHER"


def is_results_filing(item: Dict) -> bool:
    """Convenience -- Bob's deep-analysis path only fires for results."""
    return classify_us_filing(item) == "RESULTS_HY_FY"
