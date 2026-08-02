from src.memo_generator import build_memo_text


def _base_kwargs(**overrides):
    kwargs = dict(
        company={"ticker": "BHP", "company_name": "BHP Group"},
        summary={"distance_to_high_pct": 2.0, "alert_tier": "Tier 3: Deep Review"},
        reasons=["within 5.0% of 52-week high"],
        doubts=[],
        decision_framing="Trim candidate",
    )
    kwargs.update(overrides)
    return kwargs


def test_news_section_present_with_no_items():
    memo = build_memo_text(**_base_kwargs(significant_news=[], lookback_days=90))
    assert "Recent Significant ASX Announcements (last 90 days)" in memo
    assert "No significant announcements found." in memo


def test_news_section_defaults_when_not_passed():
    """significant_news/lookback_days are optional — memo still renders the section."""
    memo = build_memo_text(**_base_kwargs())
    assert "Recent Significant ASX Announcements (last 90 days)" in memo
    assert "No significant announcements found." in memo


def test_news_section_lists_items():
    items = [
        {
            "date": "10/06/2026",
            "title": "Trading Halt",
            "category": "Trading halt / suspension",
            "price_sensitive": True,
            "url": "https://www.asx.com.au/a.pdf",
        },
        {
            "date": "01/06/2026",
            "title": "FY26 Guidance Update",
            "category": "Guidance / trading update",
            "price_sensitive": False,
            "url": "https://www.asx.com.au/b.pdf",
        },
    ]
    memo = build_memo_text(**_base_kwargs(significant_news=items, lookback_days=90))
    assert "Trading Halt" in memo
    assert "PRICE SENSITIVE" in memo
    assert "FY26 Guidance Update" in memo
    assert "https://www.asx.com.au/a.pdf" in memo
    assert "Guidance / trading update" in memo


def test_claude_section_renumbered_to_8():
    """The AI section must come after the new news section (##7), so it's now ##8."""
    memo = build_memo_text(
        **_base_kwargs(
            significant_news=[],
            lookback_days=90,
            claude_analysis={
                "verdict": "v", "bull_case": "b", "bear_case": "b2",
                "what_must_be_true": "w", "recommendation": "r",
            },
        )
    )
    assert "## 8. Claude AI Analysis" in memo
    news_idx = memo.index("## 7. Recent Significant ASX Announcements")
    ai_idx = memo.index("## 8. Claude AI Analysis")
    assert news_idx < ai_idx
