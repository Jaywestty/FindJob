#!/usr/bin/env python3
"""Single daily orchestrator. Run:  uv run run_daily.py

Steps: ingest (existing logic) -> rescore new rows only -> Telegram digest
of this run's genuinely new jobs -> purge stale unapplied rows -> summary.

Telegram credentials come from the environment (.env supported).
"""
from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("run_daily")


def _max_rowid(db_path: str) -> int:
    with sqlite3.connect(db_path) as conn:
        try:
            return conn.execute("SELECT MAX(rowid) FROM jobs").fetchone()[0] or 0
        except sqlite3.OperationalError:  # table does not exist yet
            return 0


def _new_rows(db_path: str, since_rowid: int) -> list[dict]:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT rowid AS id, title, company, match_percentage, url "
            "FROM jobs WHERE rowid > ? "
            "ORDER BY match_percentage DESC NULLS LAST, rowid DESC",
            (since_rowid,)).fetchall()]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Daily ingest -> score -> alert -> purge.")
    p.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    p.add_argument("--profile", default=str(BASE_DIR / "profile.yaml"))
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        log.warning("python-dotenv not installed; using environment as-is")

    from ingest import main as ingest_main
    from job_ingest.config import load_config
    from job_ingest.telegram_alert import send_telegram_alert
    from job_ingest.tracking import purge_stale_jobs
    from job_match.scoring import rescore_all

    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        log.error("Bad config %s: %s", args.config, exc)
        return 2
    db_path = cfg.db_path

    # 1. ingest (existing logic, stats exposed via main.last_stats)
    before = _max_rowid(db_path)
    rc = ingest_main(["--config", args.config, "--db", db_path])
    stats = getattr(ingest_main, "last_stats", {})
    fetched = int(stats.get("fetched", 0))
    if rc != 0:
        log.error("Ingest failed (exit %d); aborting daily run", rc)
        print(f"fetched={fetched} inserted=0 purged=0 alert_sent=False")
        return rc
    inserted = len(_new_rows(db_path, before))

    # 2. score only the unscored rows (i.e. this run's inserts)
    try:
        rescore_all(db_path, args.profile, only_unscored=True)
    except Exception as exc:  # noqa: BLE001
        log.error("Rescore failed (continuing): %s: %s",
                  type(exc).__name__, exc)

    # 3. this run's new jobs only — never the whole table
    new_jobs = _new_rows(db_path, before)
    alert_sent = False
    if not new_jobs:
        log.info("No new jobs this run; skipping Telegram alert")
    else:
        bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not bot_token or not chat_id:
            log.warning("Telegram alert skipped: TELEGRAM_BOT_TOKEN / "
                        "TELEGRAM_CHAT_ID not set")
        else:
            alert_sent = send_telegram_alert(new_jobs, bot_token, chat_id,
                                             top_n=cfg.telegram_top_n)

    # 4. purge stale unapplied rows
    try:
        purged = purge_stale_jobs(db_path, cfg.retention_days)
    except Exception as exc:  # noqa: BLE001
        log.error("Purge failed: %s: %s", type(exc).__name__, exc)
        purged = 0

    print(f"fetched={fetched} inserted={inserted} purged={purged} "
          f"alert_sent={alert_sent}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
