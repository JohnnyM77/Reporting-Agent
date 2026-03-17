"""Claude-powered investment analysis for tickers flagged near their 52-week low.

This module provides a buy-opportunity analysis to complement the valuation
workbook when a ticker has no pre-built valuations/<ticker>.yaml config.
It mirrors the pattern used in sunday-sally/src/claude_analyst.py but focuses
on 52-week LOW events (potential value entry) rather than overvaluation alerts.
"""
from __future__ import annotations

import os


def _log(msg: str) -> None:
    print(f"[wally/claude_analyst] {msg}", flush=True)


def analyse_opportunity(
    ticker: str,
    company_name: str,
    summary: dict,
    reasons: list[str],
    news: list[str] | None = None,
) -> dict | None:
    """Call Claude to produce a structured analysis for a ticker near its 52-week low.

    Returns a dict with keys: verdict, bull_case, bear_case,
    what_must_be_true, recommendation — or None if the API key is absent
    or the call fails (non-fatal).
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

    prompt = f"""You are Wally the Watcher, a disciplined long-term value investor reviewing global equities.
A company has been flagged because its share price is trading near its 52-week low.
Your job is to write a concise but rigorous investment analysis assessing whether this represents a genuine buying opportunity or a value trap.

Company: {company_name} ({ticker})
Current price: {summary.get('current_price')}
52-week low: {summary.get('low_52w')}
52-week high: {summary.get('high_52w')}
Distance above 52-week low: {summary.get('distance_to_low_pct')}%

Valuation metrics:
  Trailing PE: {summary.get('trailing_pe')}
  Forward PE: {summary.get('forward_pe')}
  EV/EBITDA: {summary.get('ev_ebitda')}
  Price/Sales: {summary.get('price_to_sales')}
  FCF Yield: {summary.get('fcf_yield')}
  Dividend Yield: {summary.get('dividend_yield')}

Screening trigger: {'; '.join(reasons) if reasons else 'Trading near 52-week low'}{news_block}

Please respond in the following exact format (fill in each section):

VERDICT: [one sentence summary of whether this looks like a genuine opportunity or a value trap]

BULL CASE: [2-3 sentences on what would make this a compelling buy at the current price]

BEAR CASE: [2-3 sentences on the key risks and why the stock might continue falling]

WHAT MUST BE TRUE: [2-3 specific, falsifiable conditions that must hold for the current price to be a genuine buying opportunity]

RECOMMENDATION: [one clear action recommendation: Monitor / Research further / Buy on conviction, with brief rationale]"""

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
