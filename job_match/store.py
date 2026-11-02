"""SQLite helpers for scoring: match_percentage column + bulk read/write."""
from __future__ import annotations

import sqlite3


def ensure_match_column(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "match_percentage" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN match_percentage REAL")
        # Tolerate DBs that predate the region/remote_scope columns (same
        # auto-migration pattern as job_ingest/db.py).
        if "region" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN region TEXT")
        if "remote_scope" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN remote_scope TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_match ON jobs(match_percentage)"
        )
        conn.commit()


def fetch_jobs_for_scoring(db_path: str,
                           only_unscored: bool = False,
                           limit: int | None = None) -> list[dict]:
    """Rows needed for scoring. Newest postings first (posted_date may be NULL)."""
    ensure_match_column(db_path)
    q = ("SELECT url, title, description, location, is_remote, "
         "COALESCE(remote_scope, '') AS remote_scope, match_percentage "
         "FROM jobs")
    if only_unscored:
        q += " WHERE match_percentage IS NULL"
    q += " ORDER BY posted_date DESC NULLS LAST, rowid DESC"
    if limit is not None:
        q += f" LIMIT {int(limit)}"
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(q).fetchall()]


def save_scores(db_path: str, scores: dict[str, float]) -> int:
    """Write url -> match_percentage. Returns rows updated."""
    if not scores:
        return 0
    ensure_match_column(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "UPDATE jobs SET match_percentage = ? WHERE url = ?",
            [(float(v), u) for u, v in scores.items()],
        )
        conn.commit()
        return conn.total_changes


def top_matches(db_path: str, n: int = 10) -> list[dict]:
    ensure_match_column(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(r) for r in conn.execute(
            "SELECT title, company, location, source, match_percentage FROM jobs "
            "WHERE match_percentage IS NOT NULL "
            "ORDER BY match_percentage DESC LIMIT ?", (int(n),)).fetchall()]
