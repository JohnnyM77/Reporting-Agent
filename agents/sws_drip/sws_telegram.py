"""
agents/sws_drip/sws_telegram.py
────────────────────────────────
Telegram notification helper. No-op if env vars are missing.
"""

from __future__ import annotations

import os
import urllib.request
import urllib.parse
import json


def notify(message: str, urgent: bool = False) -> None:
    """
    Send a Telegram message via TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID.
    Silent no-op if either env var is missing or the request fails.
    """
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if not bot_token or not chat_id:
        return

    prefix = "🚨 " if urgent else "📊 "
    text = f"{prefix}*SWS Drip Bot*\n{message}"

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
        "disable_notification": not urgent,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                print(f"[sws_telegram] Telegram returned HTTP {resp.status}", flush=True)
    except Exception as e:
        print(f"[sws_telegram] Failed to send Telegram notification: {e}", flush=True)
