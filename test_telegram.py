#!/usr/bin/env python3
"""
test_telegram.py
----------------
Quick local test — run this from the repo root to verify your Telegram
bot token and chat ID are working before deploying to GitHub Actions.

Usage:
    export TELEGRAM_BOT_TOKEN="your_bot_token_here"
    export TELEGRAM_CHAT_ID="your_chat_id_here"
    python test_telegram.py

Optional: pass a real file path as first argument to test document sending:
    python test_telegram.py some_report.xlsx
"""

import os
import sys
import tempfile
from pathlib import Path

# Make sure we can import telegram_sender from this folder
sys.path.insert(0, str(Path(__file__).parent))

from telegram_sender import send_message, send_document, send_run_summary


def check_env() -> bool:
    ok = True
    for var in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        if not os.environ.get(var, "").strip():
            print(f"❌  {var} is not set. Export it before running this script.")
            ok = False
    return ok


def main() -> None:
    print("── Telegram delivery test ──")

    if not check_env():
        sys.exit(1)

    # 1. Simple text message
    print("\n[1] Sending test text message...")
    result = send_message(
        "<b>🤖 Telegram delivery test</b>\n\nIf you can read this, your bot token and chat ID are working correctly.",
        raise_on_error=True,
    )
    print(f"    OK — message_id: {result.get('result', {}).get('message_id')}")

    # 2. Document test
    file_arg = sys.argv[1] if len(sys.argv) > 1 else None

    if file_arg:
        print(f"\n[2] Sending provided file: {file_arg}")
        send_document(file_arg, caption=f"Test file: {Path(file_arg).name}", raise_on_error=True)
        print("    OK — file sent.")
    else:
        # Create a tiny temp text file and send it
        print("\n[2] No file argument given — creating a temporary test file...")
        with tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False, prefix="telegram_test_") as f:
            f.write("This is a test file sent by telegram_sender.py\n")
            tmp_path = f.name

        try:
            send_document(tmp_path, caption="Test attachment from telegram_sender", raise_on_error=True)
            print(f"    OK — temp file sent: {tmp_path}")
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    # 3. Combined send_run_summary helper
    print("\n[3] Testing send_run_summary helper...")
    send_run_summary(
        summary_text="<b>✅ Test run complete</b>\n\nAll delivery checks passed.",
        attachments=[],
        agent_name="TestAgent",
        raise_on_error=True,
    )
    print("    OK")

    print("\n✅  All tests passed. Check your Telegram chat for 3 messages.")


if __name__ == "__main__":
    main()
