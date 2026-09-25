"""
sgx_classify.py -- SGX announcement classifier.

SGX assigns every announcement a structured category via two fields on the
API response: `cat` (top-level bucket) and `sub` (subcategory code). Also
`category_name` (human-readable, e.g. "Financial Statements", "General
Announcement", "Share Buy Back-On Market").

That structured metadata is a much cleaner classification signal than
ASX's title regex — which is why Bob's ASX classifier caught BXB's FY26
release only after being rewritten (see agent.py notes). We use it here
directly and fall back to title-regex ONLY for unrecognised codes.

Output buckets match Bob's ASX classifier's shape so the downstream
switch/case stays the same:

  - "RESULTS_HY_FY"           -> financial statements / results release
  - "ACQUISITION"             -> M&A activity
  - "CAPITAL_OR_DEBT_RAISE"   -> placement, rights issue, notes, convertible
  - "TRADING_UPDATE"          -> guidance, quarterly, profit warning
  - "DIVIDEND"                -> dividend / distribution notices (new for SGX)
  - "SHARE_BUYBACK"           -> on-market share buy-back notices (new for SGX)
  - "CONTRACT_MATERIAL"       -> material contract award / termination
  - "OTHER"                   -> everything else (FYI stream)

SGX category codes observed live and their mappings — grow this table as
new codes turn up rather than expanding the regex fallback:

  ANNC13  Share Buy Back-On Market                    SHARE_BUYBACK
  ANNC15  Employee Stock Option / Share Scheme        OTHER
  ANNC17  Financial Statements and Related            RESULTS_HY_FY
  ANNC18  General Announcement                        (fallback to title)
  ANNC22  Corporate Governance Report                 OTHER
  DIVD    Cash Dividend / Distribution                DIVIDEND
"""

from __future__ import annotations

import re
from typing import Dict, Optional


# --- known SGX sub codes -----------------------------------------------------
# Direct hits: anything in here bypasses title analysis entirely.
_SGX_SUB_TO_BUCKET: Dict[str, str] = {
    "ANNC13": "SHARE_BUYBACK",
    "ANNC17": "RESULTS_HY_FY",
    "ANNC22": "OTHER",
    "ANNC15": "OTHER",
    "DIVD":   "DIVIDEND",
}

# category_name matches, case-insensitive substring. SGX sometimes labels the
# same sub differently across product types; keying off category_name catches
# codes we haven't seen yet.
_CATEGORY_NAME_HITS = (
    ("financial statements",        "RESULTS_HY_FY"),
    ("full year results",           "RESULTS_HY_FY"),
    ("half year results",           "RESULTS_HY_FY"),
    ("quarterly financial",         "RESULTS_HY_FY"),
    ("preliminary results",         "RESULTS_HY_FY"),
    ("annual report",               "RESULTS_HY_FY"),
    ("interim results",             "RESULTS_HY_FY"),
    ("cash dividend",               "DIVIDEND"),
    ("distribution",                "DIVIDEND"),
    ("share buy back",              "SHARE_BUYBACK"),
    ("share buyback",               "SHARE_BUYBACK"),
    ("rights issue",                "CAPITAL_OR_DEBT_RAISE"),
    ("placement",                   "CAPITAL_OR_DEBT_RAISE"),
    ("convertible",                 "CAPITAL_OR_DEBT_RAISE"),
    ("notes issue",                 "CAPITAL_OR_DEBT_RAISE"),
    ("acquisition",                 "ACQUISITION"),
    ("takeover",                    "ACQUISITION"),
    ("scheme of arrangement",       "ACQUISITION"),
    ("profit guidance",             "TRADING_UPDATE"),
    ("profit warning",              "TRADING_UPDATE"),
    ("trading update",              "TRADING_UPDATE"),
)


# --- title fallback (for cat=ANNC / sub=ANNC18 general announcements) --------
# SGX prefixes titles with the category, e.g.
#   "General Announcement::DBS Categorically Rejects Claim"
# So we look at what's AFTER the "::". Most title analysis mirrors what
# `agent.py::classify_from_title_only` does for ASX.
_TITLE_HARD_NO = (
    "transcript", "webcast", "conference call", "investor call",
    "results of meeting", "voting results", "proxy form",
    "notice of meeting", "notice of annual general meeting",
    "notice of extraordinary",
)

# Anything on either side (results doc noun + reporting period) → RESULTS.
_TITLE_RESULTS = tuple(re.compile(p) for p in (
    r"\bresults?\s+(?:announcement|presentation|release|briefing|report)\b",
    r"\b(?:half|full)[\s\-]?year[\s\-\w&]*"
    r"\b(?:results?|report|accounts?|release|presentation)\b",
    r"\b(?:[12]h\s?)?(?:fy|hy)\s?\d{2,4}\b[\s\-\w&]*\bresults?\b",
    r"\bfinancial\s+statements?\b",
    r"\bpreliminary\s+(?:final\s+)?results?\b",
))

_TITLE_KEYWORDS = (
    ("ACQUISITION", (
        "acquisition", "acquire", "merger", "scheme of arrangement",
        "takeover", "transaction",
    )),
    ("CAPITAL_OR_DEBT_RAISE", (
        "placement", "rights issue", "capital raising", "convertible",
        "debt facility", "refinance", "term loan", "bond issue",
        "note issue", "notes issue", "senior notes",
    )),
    ("TRADING_UPDATE", (
        "trading update", "profit guidance", "profit warning",
        "earnings revision", "outlook update", "forecast update",
        "revenue update", "quarterly update",
    )),
    ("DIVIDEND", (
        "cash dividend", "dividend", "distribution notice",
    )),
    ("CONTRACT_MATERIAL", (
        "material contract", "contract award", "contract termination",
    )),
)


def _title_after_prefix(title: str) -> str:
    """SGX titles come as 'Category::Actual Headline'. Strip the prefix so
    the regex is scanning the substantive part, not the category label."""
    if "::" in title:
        return title.split("::", 1)[1].strip()
    return title.strip()


def _classify_from_title(title: str) -> str:
    """Fallback title-based classification for announcements whose sub/
    category_name doesn't pin them down (mostly the ANNC18 'General
    Announcement' bucket, which by design covers everything else)."""
    body = _title_after_prefix(title).lower()
    if not body:
        return "OTHER"
    if any(x in body for x in _TITLE_HARD_NO):
        return "OTHER"
    if any(p.search(body) for p in _TITLE_RESULTS):
        return "RESULTS_HY_FY"
    for bucket, keywords in _TITLE_KEYWORDS:
        if any(k in body for k in keywords):
            return bucket
    return "OTHER"


def classify_sgx_announcement(item: Dict) -> str:
    """Return the Bob-shaped classification bucket for one SGX announcement
    dict (as produced by sgx_fetch._normalize_row). Consults, in order:

      1. The exact `sub` code (fastest, most reliable when known).
      2. `category_name` substring hits (catches codes we haven't cataloged).
      3. Title regex fallback (for ANNC18 General Announcement etc.).

    Never returns None — falls back to "OTHER" so downstream code can
    unconditionally switch on the result."""
    sub = (item.get("sub") or "").strip().upper()
    if sub in _SGX_SUB_TO_BUCKET:
        return _SGX_SUB_TO_BUCKET[sub]

    cat_name = (item.get("category_name") or "").lower()
    for needle, bucket in _CATEGORY_NAME_HITS:
        if needle in cat_name:
            return bucket

    title = item.get("title") or ""
    return _classify_from_title(title)


def is_results_announcement(item: Dict) -> bool:
    """Convenience — Bob's deep-analysis path only fires for results."""
    return classify_sgx_announcement(item) == "RESULTS_HY_FY"


def is_price_sensitive_category(item: Dict) -> bool:
    """SGX doesn't ship a boolean price-sensitive flag on the announcements
    endpoint, so we approximate: anything Bob would ordinarily deep-dive
    into is price-sensitive; general/employee-scheme/governance are not.
    Callers wanting stricter filtering should key off the category directly."""
    bucket = classify_sgx_announcement(item)
    return bucket in (
        "RESULTS_HY_FY", "ACQUISITION", "CAPITAL_OR_DEBT_RAISE",
        "TRADING_UPDATE", "CONTRACT_MATERIAL",
    )
