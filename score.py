#!/usr/bin/env python3
"""CLI: uv run score.py [--profile profile.yaml] [--db jobs.db] [--rescore]

Scores jobs in SQLite against your profile (embeddings + rules) and stores
match_percentage back on each row. No ingestion involved.

  uv run score.py              # score only previously unscored jobs
  uv run score.py --rescore    # re-score every job (e.g. after profile edits)
  uv run score.py --limit 100  # score a subset (newest first)
"""
from __future__ import annotations

import argparse
import logging
import sys

from job_match.scoring import rescore_all
from job_match.store import top_matches

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("score")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Score jobs against your profile.")
    p.add_argument("--profile", default="profile.yaml", help="Match profile YAML")
    p.add_argument("--db", default=None, help="SQLite path (default: config.yaml db_path)")
    p.add_argument("--rescore", action="store_true", help="Re-score all jobs, not just unscored")
    p.add_argument("--limit", type=int, default=None, help="Max jobs to score (newest first)")
    p.add_argument("--top", type=int, default=10, help="Show top-N matches after scoring")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    db_path = args.db
    if db_path is None:
        try:
            from job_ingest.config import load_config
            db_path = load_config("config.yaml").db_path
        except Exception:  # noqa: BLE001
            db_path = "jobs.db"
    try:
        stats = rescore_all(db_path, args.profile,
                            only_unscored=not args.rescore, limit=args.limit)
    except Exception as exc:  # noqa: BLE001
        log.error("Scoring failed: %s: %s", type(exc).__name__, exc)
        return 1

    print(f"\nScored {stats['scored']} jobs "
          f"(mean match {stats['mean_match']}%, model {stats['model']})")
    print(f"=== Top {args.top} matches ===")
    for i, m in enumerate(top_matches(db_path, args.top), 1):
        print(f"{i:2d}. {m['match_percentage']:5.1f}% | {m['title']} "
              f"@ {m['company']} [{m['location']}] ({m['source']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
