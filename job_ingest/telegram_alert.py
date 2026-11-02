"""Telegram alerts for new matched jobs + one-time setup confirmation.

Credentials always come from the environment (TELEGRAM_BOT_TOKEN /
TELEGRAM_CHAT_ID, via a .env file); never hardcoded here.
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

import requests

log = logging.getLogger(__name__)

SEND_URL = "https://api.telegram.org/bot{token}/sendMessage"
TIMEOUT = 20
# Telegram caps one message at 4096 chars; stay safely under it.
MAX_MESSAGE_CHARS = 3500

# One-time welcome text. Edit freely — setup_telegram.py sends this once.
WELCOME_MESSAGE = (
    "Welcome to your personal job hunting tool. "
    "Live updates arrive daily at 11am with your newest, best-matched jobs."
)


def _post(bot_token: str, chat_id: str, text: str) -> bool:
    """POST one message; log (never raise) on failure. Returns success."""
    try:
        resp = requests.post(
            SEND_URL.format(token=bot_token),
            json={"chat_id": chat_id, "text": text,
                  "disable_web_page_preview": True},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            log.error("Telegram rejected message: %s", data)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("Telegram send failed: %s: %s", type(exc).__name__, exc)
        return False


def _score(job: Mapping[str, Any] | Any) -> float:
    if isinstance(job, Mapping):
        v = job.get("match_percentage")
    else:
        v = getattr(job, "match_percentage", None)
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _field(job: Mapping[str, Any] | Any, name: str, default: str = "") -> str:
    if isinstance(job, Mapping):
        return str(job.get(name) or default)
    return str(getattr(job, name, default) or default)


def send_telegram_alert(jobs: list, bot_token: str, chat_id: str,
                        top_n: int = 10) -> bool:
    """Summarize new jobs: count + top N by match_percentage.

    Each entry lists title, company, match_percentage and url.
    Failures are logged, never raised. Returns True on success.
    """
    if not bot_token or not chat_id:
        log.error("Telegram alert skipped: missing bot token or chat id")
        return False
    ranked = sorted(jobs, key=_score, reverse=True)[:max(int(top_n), 0)]
    lines = [f"New matched jobs: {len(jobs)}"]
    for i, job in enumerate(ranked, 1):
        lines.append(
            f"\n{i}. {_field(job, 'title', 'Untitled')} "
            f"@ {_field(job, 'company', '?')} "
            f"({_score(job):.1f}%)\n{_field(job, 'url')}")
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[:MAX_MESSAGE_CHARS] + "\n…(truncated)"
    ok = _post(bot_token, chat_id, text)
    if ok:
        log.info("Telegram alert sent (%d jobs, listed %d)",
                 len(jobs), len(ranked))
    return ok


def send_setup_confirmation(bot_token: str, chat_id: str) -> bool:
    """Send the one-time welcome message. Returns True on success."""
    if not bot_token or not chat_id:
        log.error("Telegram setup confirmation skipped: "
                  "missing bot token or chat id")
        return False
    ok = _post(bot_token, chat_id, WELCOME_MESSAGE)
    if ok:
        log.info("Telegram setup confirmation sent")
    return ok
