"""Wally's Shortlist — scoring gate, LLM ranking, and rendering.

yfinance and the Anthropic client are injected, so these run offline.
"""
from wally.data_fetch import ValuationSnapshot
from wally.screening import TickerScreenResult
from wally import shortlist as S


def _row(ticker, name="Co", price=5.0, lo=4.0, hi=10.0, near_low=False,
         target=None, below=False, below_high=0.0, error=None):
    return TickerScreenResult(
        ticker=ticker, company_name=name, current_price=price, low_52w=lo,
        high_52w=hi, distance_to_low_pct=50.0, below_high_pct=below_high,
        flagged=(near_low or below), near_low=near_low, target_price=target,
        below_target=below, distance_to_target_pct=None, error=error,
    )


class _Resp:
    def __init__(self, text):
        self.text = text


def _fetch(mapping):
    def f(ticker):
        return mapping.get(ticker, ValuationSnapshot(None, None, None, None, None, None))
    return f


def test_expensive_gate_caps_the_score():
    """A stock that fell from 80x to 60x is still expensive — the gate applies."""
    r = _row("EXP.AX", below=True, target=90, below_high=40)  # de-rated + below buy price
    val = ValuationSnapshot(70, 60, 40, 20, 0.005, 0.0)
    score, notes = S._opportunity_score(r, val)
    assert score <= 5.0, f"expensive name not gated: {score}"
    assert "still expensive" in notes


def test_cheap_cashflow_name_scores_above_pricey_derating():
    cheap = S._opportunity_score(_row("CHP.AX", near_low=True, below_high=50),
                                 ValuationSnapshot(11, 10, 7, 1.2, 0.09, 0.05))[0]
    pricey = S._opportunity_score(_row("EXP.AX", below=True, target=90, below_high=45),
                                  ValuationSnapshot(70, 60, 40, 20, 0.005, 0.0))[0]
    assert cheap > pricey


def test_dividend_yield_percent_or_fraction_normalises():
    assert abs(S._as_fraction(4.5) - 0.045) < 1e-9   # percent form
    assert abs(S._as_fraction(0.045) - 0.045) < 1e-9  # fraction form
    assert S._as_fraction(None) is None


def test_rows_with_errors_are_skipped():
    rows = [(_row("BAD.AX", error="No market data"), "JM Watch List")]
    cands = S._rank_candidates(rows, fetch_valuation=_fetch({}))
    assert cands == []


def test_pipeline_ranks_and_maps_only_known_tickers():
    rows = [
        (_row("CHP.AX", near_low=True, below_high=50), "JM Watch List"),
        (_row("DIV.AX", near_low=True, below_high=30), "Income Watchlist"),
    ]
    vals = {"CHP.AX": ValuationSnapshot(11, 10, 7, 1.2, 0.09, 0.05),
            "DIV.AX": ValuationSnapshot(13, 12, 8, 1.5, 0.07, 0.065)}

    def send(client, **kw):
        # includes an unknown ticker that must be dropped, and a code fence
        return _Resp('```json\n[{"ticker":"CHP.AX","rank":1,"one_liner":"a","thesis":"b",'
                     '"why_now":"c","key_risk":"d","verdict":"Strong buy candidate"},'
                     '{"ticker":"ZZZ.AX","rank":2,"one_liner":"x","thesis":"y","why_now":"z",'
                     '"key_risk":"w","verdict":"Watch closely"}]\n```')

    res = S.build_shortlist(rows, fetch_valuation=_fetch(vals),
                            llm_send=send, make_client=lambda k: object())
    assert res.status == "ok"
    assert [p.ticker for p in res.picks] == ["CHP.AX"]  # unknown ZZZ dropped
    assert res.picks[0].forward_pe == 10


def test_empty_reply_is_honest_not_padded():
    rows = [(_row("CHP.AX", near_low=True), "JM Watch List")]
    res = S.build_shortlist(rows, fetch_valuation=_fetch({}),
                            llm_send=lambda c, **k: _Resp("[]"), make_client=lambda k: object())
    assert res.picks == []
    assert "sitting on hands" in S.render_email_html(res, "2026-09-26")


def test_email_is_inline_only_no_flex_grid_style_class():
    rows = [(_row("CHP.AX", near_low=True), "JM Watch List")]
    vals = {"CHP.AX": ValuationSnapshot(11, 10, 7, 1.2, 0.09, 0.05)}
    res = S.build_shortlist(
        rows, fetch_valuation=_fetch(vals),
        llm_send=lambda c, **k: _Resp('[{"ticker":"CHP.AX","rank":1,"one_liner":"a",'
                                       '"thesis":"b","why_now":"c","key_risk":"d","verdict":"Worth a look"}]'),
        make_client=lambda k: object())
    html = S.render_email_html(res, "2026-09-26")
    for banned in ("display:flex", "display:grid", "<style", "class="):
        assert banned not in html, f"email contains {banned}"
    assert "CHP.AX" in html and "Why now" in html


def test_no_api_key_skips_without_raising(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    rows = [(_row("CHP.AX", near_low=True), "JM Watch List")]
    res = S.build_shortlist(rows, fetch_valuation=_fetch({}))  # real path, no key
    assert res.status == "skipped"
    assert res.picks == []
