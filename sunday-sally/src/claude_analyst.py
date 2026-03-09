from __future__ import annotations

import os

_ANALYSIS_KEYS = ("verdict", "bull_case", "bear_case", "what_must_be_true", "recommendation")


def _log(msg: str) -> None:
    print(f"[claude_analyst] {msg}", flush=True)


def analyse_company(
    ticker: str,
    company_name: str,
    summary: dict,
    reasons: list[str],
    news: list[str] | None = None,
) -> dict | None:
    """
    Call Claude (claude-opus-4-6) to produce a structured valuation analysis
    for a flagged company.

    Returns a dict with keys: verdict, bull_case, bear_case,
    what_must_be_true, recommendation — or None if the API key is absent or
    the call fails (non-fatal).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        _log("ANTHROPIC_API_KEY not set — skipping AI analysis.")
        return None

    try:
        import anthropic
    except ImportError:
        _log("anthropic package not installed — skipping AI analysis.")
        return None

    news_block = ""
    if news:
        headlines = "\n".join(f"  - {h}" for h in news[:10])
        news_block = f"\nRecent news headlines:\n{headlines}"

    prompt = f"""You are Sunday Sally, a disciplined value investor reviewing Australian ASX-listed equities.
A company has been flagged as potentially over-valued based on quantitative screens.
Your job is to write a concise but rigorous investment analysis.

Company: {company_name} ({ticker})
Current price: ${summary.get('current_price')}
52-week high: ${summary.get('high_52w')}
Distance to 52-week high: {summary.get('distance_to_high_pct')}%
Alert tier: {summary.get('alert_tier')}

Valuation metrics:
  Trailing PE: {summary.get('trailing_pe')}
  Forward PE: {summary.get('forward_pe')}
  EV/EBITDA: {summary.get('ev_ebitda')}
  Price/Sales: {summary.get('price_to_sales')}
  FCF Yield: {summary.get('fcf_yield')}
  Dividend Yield: {summary.get('dividend_yield')}

Historical PE context:
  3-year avg PE: {summary.get('pe_3y_avg')}
  5-year avg PE: {summary.get('pe_5y_avg')}
  10-year avg PE: {summary.get('pe_10y_avg')}
  Valuation percentile vs own history: {summary.get('valuation_percentile')} (higher = more expensive)

Quantitative trigger reasons: {'; '.join(reasons) if reasons else 'Near 52-week high'}
{news_block}

Please respond in the following exact format (fill in each section):

VERDICT: [one sentence summary of current valuation attractiveness]

BULL CASE: [2-3 sentences on what would justify the current price / make the stock a hold]

BEAR CASE: [2-3 sentences on the key risks and why the stock may be expensive]

WHAT MUST BE TRUE: [2-3 specific, falsifiable conditions that must hold for the current price to be fair value]

RECOMMENDATION: [one clear action recommendation: Watch Only / Hold but stop adding / Trim position, with brief rationale]"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        _log(f"Requesting Claude analysis for {ticker}…")
        response = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            thinking={"type": "adaptive"},
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract the text block (skip thinking blocks)
        text = next(
            (block.text for block in response.content if block.type == "text"),
            "",
        )

        if not text:
            _log(f"No text in response for {ticker}.")
            return None

        result: dict[str, str] = {}
        for key, label in [
            ("verdict", "VERDICT:"),
            ("bull_case", "BULL CASE:"),
            ("bear_case", "BEAR CASE:"),
            ("what_must_be_true", "WHAT MUST BE TRUE:"),
            ("recommendation", "RECOMMENDATION:"),
        ]:
            start = text.find(label)
            if start == -1:
                result[key] = ""
                continue
            start += len(label)
            # Find next section label or end of string
            next_section = len(text)
            for _, other_label in [
                ("verdict", "VERDICT:"),
                ("bull_case", "BULL CASE:"),
                ("bear_case", "BEAR CASE:"),
                ("what_must_be_true", "WHAT MUST BE TRUE:"),
                ("recommendation", "RECOMMENDATION:"),
            ]:
                pos = text.find(other_label, start)
                if pos != -1 and pos < next_section:
                    next_section = pos
            result[key] = text[start:next_section].strip()

        _log(f"Claude analysis complete for {ticker}.")
        return result

    except Exception as exc:
        _log(f"Claude API call failed for {ticker}: {exc}")
        return None
