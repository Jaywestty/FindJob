#!/usr/bin/env python3
"""One-time Telegram setup check. Run manually ONCE after setup:

  uv run setup_telegram.py

Reads TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID from the environment (.env
supported) and sends the welcome message. Not called from run_daily.py.
"""
from __future__ import annotations

import logging
import os
import sys

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("setup_telegram")


def main() -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        log.warning("python-dotenv not installed; using environment as-is")

    from job_ingest.telegram_alert import send_setup_confirmation

    bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        print("FAILED: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID "
              "(environment or .env file)")
        return 1
    if send_setup_confirmation(bot_token, chat_id):
        print("SUCCESS: welcome message sent — check Telegram")
        return 0
    print("FAILED: Telegram rejected the request or network error "
          "(see log above)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
