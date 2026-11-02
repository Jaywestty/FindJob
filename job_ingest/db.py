"""SQLite persistence. Skips rows that already exist by URL."""
from __future__ import annotations

import logging
import sqlite3

from .dedup import dedup_hash
from .models import Job
from .tracking import ensure_tracking_columns

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    url TEXT PRIMARY KEY,
    title TEXT,
    company TEXT,
    description TEXT,
    location TEXT,
    posted_date TEXT,
    source TEXT,
    is_remote INTEGER,
    salary_min REAL,
    salary_max REAL,
    dedup_hash TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    applied INTEGER DEFAULT 0,
    applied_date TIMESTAMP,
    interview_status TEXT,
    notes TEXT,
    seniority TEXT,
    region TEXT,
    remote_scope TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_dedup_hash ON jobs(dedup_hash);
CREATE INDEX IF NOT EXISTS idx_jobs_source ON jobs(source);
CREATE TABLE IF NOT EXISTS ingestion_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    total_fetched INTEGER DEFAULT 0,
    total_kept INTEGER DEFAULT 0
);
"""


def record_ingestion_run(db_path: str, total_fetched: int,
                         total_kept: int) -> int:
    """Append one row per ingest run. Returns the new run id."""
    init_db(db_path)
    with sqlite3.connect(db_path) as conn:
        cur = conn.execute(
            "INSERT INTO ingestion_runs (total_fetched, total_kept) "
            "VALUES (?, ?)", (int(total_fetched), int(total_kept)))
        conn.commit()
        return cur.lastrowid


def init_db(db_path: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
    # Migrate pre-existing DBs that predate the tracking columns.
    ensure_tracking_columns(db_path)
    # Migrate DBs created before the seniority/region/remote_scope
    # columns existed (same auto-migration pattern as tracking columns).
    with sqlite3.connect(db_path) as conn:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        if "seniority" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN seniority TEXT")
        if "region" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN region TEXT")
        if "remote_scope" not in cols:
            conn.execute("ALTER TABLE jobs ADD COLUMN remote_scope TEXT")
        conn.commit()


def save_jobs(jobs: list[Job], db_path: str) -> tuple[int, int]:
    """Insert jobs, skipping existing URLs. Returns (inserted, skipped)."""
    if not jobs:
        return 0, 0
    init_db(db_path)
    inserted = 0
    skipped = 0
    rows = [
        (
            j.url, j.title, j.company, j.description, j.location,
            j.posted_date, j.source, int(bool(j.is_remote)),
            j.salary_min, j.salary_max, dedup_hash(j), j.seniority,
            (j.region or "").strip().lower() or None,
            (j.remote_scope or "").strip().lower() or None,
        )
        for j in jobs
        if j.url  # URL is the primary key; drop rows without one
    ]
    skipped += len(jobs) - len(rows)
    with sqlite3.connect(db_path) as conn:
        for row in rows:
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO jobs
                       (url, title, company, description, location, posted_date,
                        source, is_remote, salary_min, salary_max, dedup_hash,
                        seniority, region, remote_scope, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                               CURRENT_TIMESTAMP)""",
                    row,
                )
                if cur.rowcount and cur.rowcount > 0:
                    inserted += 1
                else:
                    skipped += 1
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipping row %s: %s", row[0], exc)
                skipped += 1
        conn.commit()
    return inserted, skipped
