#!/usr/bin/env python3
"""One-time backfill purge: retroactively apply job_passes_filters to rows
already in jobs.db and delete the ones that no longer match.

  uv run backfill_purge.py            # dry-run: summary only, deletes nothing
  uv run backfill_purge.py --confirm  # actually delete

Separate from run_daily.py / purge_stale_jobs (age-based retention) — this
is preference matching, run once after filter rules change. Rows with
applied = 1 are NEVER deleted; application history is preserved.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from collections import Counter
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("backfill_purge")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Backfill filter purge (dry-run by default).")
    p.add_argument("--config", default=str(BASE_DIR / "config.yaml"))
    p.add_argument("--db", default=None, help="SQLite path (default: config db_path)")
    p.add_argument("--confirm", action="store_true",
                   help="Actually delete. Without it, dry-run only.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    from job_ingest.config import load_config
    from job_ingest.db import init_db
    from job_ingest.filters import job_passes_filters
    from job_ingest.models import Job

    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        log.error("Bad config %s: %s", args.config, exc)
        return 2
    db_path = args.db or cfg.db_path
    init_db(db_path)  # ensure tracking/seniority columns exist

    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT rowid AS id, url, title, company, description, location, "
            "source, COALESCE(applied, 0) AS applied FROM jobs").fetchall()

    keep_pass: list[int] = []
    keep_applied: list[int] = []
    doomed: list[tuple[int, str]] = []  # (rowid, source)
    for r in rows:
        job = Job(title=r["title"] or "", company=r["company"] or "",
                  description=r["description"] or "",
                  location=r["location"] or "", url=r["url"] or "")
        if job_passes_filters(job, cfg):
            keep_pass.append(r["id"])
        elif r["applied"]:
            keep_applied.append(r["id"])
        else:
            doomed.append((r["id"], r["source"] or "?"))

    mode = "CONFIRMED DELETE" if args.confirm else "DRY-RUN (nothing deleted)"
    print(f"\n=== Backfill purge [{mode}] ===")
    print(f"Rows checked:              {len(rows)}")
    print(f"Would delete (fail+unapplied): {len(doomed)}")
    print(f"Kept (applied=1 despite fail): {len(keep_applied)}")
    print(f"Kept (pass filter):        {len(keep_pass)}")

    if not args.confirm:
        by_source = Counter(s for _, s in doomed)
        print("--- would-remove by source ---")
        for src, n in by_source.most_common():
            print(f"  {src}: {n}")
        print("Re-run with --confirm to delete.")
        return 0

    removed_by_source = Counter(s for _, s in doomed)
    with sqlite3.connect(db_path) as conn:
        conn.executemany("DELETE FROM jobs WHERE rowid = ?",
                         [(i,) for i, _ in doomed])
        conn.commit()
        final = conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    print(f"Deleted: {len(doomed)}")
    print(f"Final row count in jobs: {final}")
    print("--- removed by source ---")
    for src, n in removed_by_source.most_common():
        print(f"  {src}: {n}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
