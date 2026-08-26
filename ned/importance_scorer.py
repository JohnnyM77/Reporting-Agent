# ned/importance_scorer.py
#
# News importance classification system for Ned the News Agent.
# Scores news items and assigns priority levels based on headline keywords.

from __future__ import annotations

# Priority levels
PRIORITY_CRITICAL = "CRITICAL"
PRIORITY_HIGH = "HIGH"
PRIORITY_MEDIUM = "MEDIUM"
PRIORITY_LOW = "LOW"
PRIORITY_FYI = "FYI"

# Keyword scoring rules.
#
# The CRITICAL/HIGH lists are deliberately broad on earnings vocabulary: during
# reporting season most portfolio news IS a result, and the dashboard caps cards
# at two-per-company, so generous flagging surfaces the right names without
# flooding. Note the singular "result" alongside "results" — headlines like
# "BHP 1H26 result" or "FY26 full-year result" are extremely common and were
# being missed (the same singular trap documented for Bob's results gate).
CRITICAL_KEYWORDS = [
    # Results / reporting-season documents
    "earnings",
    "results",
    "result",            # singular — "1H26 result", "full-year result"
    "half year",
    "half-year",
    "full year",
    "full-year",
    "first half",
    "second half",
    "interim",
    "preliminary final",
    "trading update",
    "quarterly",
    "appendix 4d",
    "appendix 4e",
    # Numbers that only appear in a result / price-sensitive update
    "profit",
    "npat",
    "ebitda",
    "revenue",
    "dividend",
    "guidance",
    "downgrade",         # profit/guidance downgrade — highly material
    # Corporate actions
    "capital raise",
    "capital raising",
    "placement",
    "acquisition",
    "takeover",
    "merger",
    "scheme of arrangement",
]

HIGH_KEYWORDS = [
    "upgrade",           # guidance/analyst upgrade
    "buyback",
    "buy-back",
    "contract",
    "partnership",
    "ceo",
    "managing director",
    "chief executive",
    "lawsuit",
    "litigation",
    "regulator",
    "asic",
    "product launch",
]

MEDIUM_KEYWORDS = [
    "analyst",
    "industry",
    "forecast",
    "broker",
]


def score_news_item(headline: str) -> int:
    """
    Score a news item based on headline keywords.
    
    Rules:
    - Each CRITICAL_KEYWORD match: +5 points
    - Each HIGH_KEYWORD match: +3 points
    - Each MEDIUM_KEYWORD match: +2 points
    - Each keyword can only contribute once (no double-counting same keyword)
    
    Args:
        headline: The news headline to score
        
    Returns:
        Total score (integer)
    """
    import re
    
    score = 0
    headline_lower = headline.lower()
    
    # Use word boundary matching to avoid false positives like 'ceo' matching 'ceolite'
    # Track which keywords were matched to avoid double-counting
    
    # Check for critical keywords (each unique match adds 5)
    for keyword in CRITICAL_KEYWORDS:
        # Use word boundaries for better matching
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, headline_lower):
            score += 5
    
    # Check for high-priority keywords (each unique match adds 3)
    for keyword in HIGH_KEYWORDS:
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, headline_lower):
            score += 3
    
    # Check for medium-priority keywords (each unique match adds 2)
    for keyword in MEDIUM_KEYWORDS:
        pattern = r'\b' + re.escape(keyword) + r'\b'
        if re.search(pattern, headline_lower):
            score += 2
    
    return score


def classify_importance(score: int) -> str:
    """
    Classify news importance based on score.
    
    Rules:
    - score >= 5: CRITICAL
    - score >= 3: HIGH
    - score >= 2: MEDIUM
    - score < 2: LOW
    
    Args:
        score: The news item score
        
    Returns:
        Priority level (CRITICAL/HIGH/MEDIUM/LOW)
    """
    if score >= 5:
        return PRIORITY_CRITICAL
    elif score >= 3:
        return PRIORITY_HIGH
    elif score >= 2:
        return PRIORITY_MEDIUM
    else:
        return PRIORITY_LOW


def score_and_classify(headline: str) -> tuple[int, str]:
    """
    Score a headline and classify its importance in one call.
    
    Args:
        headline: The news headline to evaluate
        
    Returns:
        Tuple of (score, priority_level)
    """
    score = score_news_item(headline)
    priority = classify_importance(score)
    return score, priority


def sort_by_importance(hits: list[dict]) -> list[dict]:
    """
    Sort news hits by importance (descending).
    Adds 'importance_score' and 'importance_level' to each hit.
    
    Args:
        hits: List of news hit dictionaries
        
    Returns:
        Sorted list with importance metadata added
    """
    # Score and classify each hit
    for hit in hits:
        headline = hit.get("title", "")
        score, priority = score_and_classify(headline)
        hit["importance_score"] = score
        hit["importance_level"] = priority
    
    # Sort by score descending, then by title alphabetically
    return sorted(hits, key=lambda h: (-h["importance_score"], h.get("title", "")))
