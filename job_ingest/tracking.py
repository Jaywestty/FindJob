"""Application tracking + DB cleanup for the jobs table.

Auto-migrates (same PRAGMA-check pattern as job_match/store.py):
adds created_at/applied/applied_date/interview_status/notes when missing.
"""
from __future__ import annotations

import sqlite3

VALID_STATUSES = (
    "pending",
    "no_response",
    "rejected",
    "interview_scheduled",
    "interview_completed",
    "offer",
)

# column -> ADD COLUMN DDL (applied if missing)
TRACKING_COLUMNS = {
    "created_at": "ALTER TABLE jobs ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP",
    "applied": "ALTER TABLE jobs ADD COLUMN applied INTEGER DEFAULT 0",
    "applied_date": "ALTER TABLE jobs ADD COLUMN applied_date TIMESTAMP",
    "interview_status": "ALTER TABLE jobs ADD COLUMN interview_status TEXT",
    "notes": "ALTER TABLE jobs ADD COLUMN notes TEXT",
}


def ensure_tracking_columns(db_path: str) -> None:
    """Add tracking columns if not present. Safe to call on every run."""
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        for name, ddl in TRACKING_COLUMNS.items():
            if name not in cols:
                conn.execute(ddl)
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_applied ON jobs(applied)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_created ON jobs(created_at)"
        )
        conn.commit()


def get_job(db_path: str, job_id: int) -> dict | None:
    """Full job row by rowid (including tracking fields), or None."""
    ensure_tracking_columns(db_path)
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        r = conn.execute("SELECT rowid AS id, * FROM jobs WHERE rowid = ?",
                         (job_id,)).fetchone()
        return dict(r) if r is not None else None


def mark_applied(db_path: str, job_id: int, applied: bool) -> bool:
    """Set applied flag. Marking True stamps applied_date=now (UTC);
    marking False clears it. Returns False if the id does not exist."""
    ensure_tracking_columns(db_path)
    with sqlite3.connect(db_path) as conn:
        if applied:
            cur = conn.execute(
                "UPDATE jobs SET applied = 1, applied_date = CURRENT_TIMESTAMP "
                "WHERE rowid = ?", (job_id,))
        else:
            cur = conn.execute(
                "UPDATE jobs SET applied = 0, applied_date = NULL "
                "WHERE rowid = ?", (job_id,))
        conn.commit()
        return cur.rowcount > 0


def set_interview_status(db_path: str, job_id: int,
                         status: str | None) -> bool:
    """Set interview_status (must be a VALID_STATUS; None clears it).
    Raises ValueError on invalid status. Returns False if id missing."""
    if status is not None and status not in VALID_STATUSES:
        raise ValueError(
            f"Invalid interview_status {status!r}; "
            f"must be one of {VALID_STATUSES} or None")
    ensure_tracking_columns(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE jobs SET interview_status = ? WHERE rowid = ?",
            (status, job_id))
        conn.commit()
        return cur.rowcount > 0


def set_notes(db_path: str, job_id: int, notes: str | None) -> bool:
    """Set free-text notes (blank/None clears to NULL).
    Returns False if the id does not exist."""
    ensure_tracking_columns(db_path)
    value = (notes or "").strip() or None
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE jobs SET notes = ? WHERE rowid = ?",
            (value, job_id))
        conn.commit()
        return cur.rowcount > 0


def purge_stale_jobs(db_path: str, days_threshold: int) -> int:
    """Delete rows where applied is 0 (or NULL on legacy rows) and created_at
    is older than days_threshold days. applied=1 rows are never deleted.
    Returns the count deleted."""
    if days_threshold < 0:
        raise ValueError("days_threshold must be >= 0")
    ensure_tracking_columns(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE COALESCE(applied, 0) = 0 "
            f"AND created_at < datetime('now', '-{int(days_threshold)} days')")
        conn.commit()
        return cur.rowcount
