from src.chatgpt_handoff_builder import build_handoff_payload


def _base_kwargs(**overrides):
    kwargs = dict(
        company={"ticker": "BHP", "company_name": "BHP Group"},
        valuation={"current_price": 40.0},
        history={"notes": []},
        announcements=[],
        news=[],
        run_date="2026-07-04",
    )
    kwargs.update(overrides)
    return kwargs


def test_significant_news_defaults_to_empty_list():
    payload = build_handoff_payload(**_base_kwargs())
    assert payload["significant_news"] == []


def test_significant_news_included_when_provided():
    items = [{"date": "01/06/2026", "title": "Trading Halt", "category": "Trading halt / suspension"}]
    payload = build_handoff_payload(**_base_kwargs(significant_news=items))
    assert payload["significant_news"] == items


def test_existing_fields_unaffected():
    payload = build_handoff_payload(**_base_kwargs())
    assert payload["company"]["ticker"] == "BHP"
    assert payload["recent_announcements"] == []
    assert payload["news_context"] == []
    assert "instructions" in payload
