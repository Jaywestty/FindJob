#!/usr/bin/env python3
"""JobTool local API (single-user, localhost only, no auth).

Run:  uv run uvicorn api:app --host 127.0.0.1 --port 8000

Endpoints:
  GET  /jobs?min_score=&source=&region=&applied=&date_from=&date_to=&limit=&offset=
  GET  /jobs/{id}            full detail for one job (by rowid)
  PATCH /jobs/{id}           {applied?, interview_status?, notes?} -> updated row
  GET  /filter-options       distinct sources + known regions + valid statuses
  GET  /health               status, row counts, last ingest, background tasks
  POST /ingest               run ingest.main() in background, returns immediately
  POST /rescore?only_unscored=  run rescore_all() in background, returns immediately

Region is derived from the job's location string using the same groupings
as config.yaml (location names + country_indeed per region), plus a small
documented alias map (ISO codes, extra country/city names). Jobs matching
nothing fall back to "other".
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query
from pydantic import BaseModel

BASE_DIR = Path(__file__).resolve().parent

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("api")

app = FastAPI(title="JobTool API")

# --------------------------------------------------------------------------
# Region derivation (seeded from config.yaml groupings + aliases)
# --------------------------------------------------------------------------

# Extra keywords per region beyond what config.yaml location names provide:
# ISO codes, other major countries/cities in the same bucket.
REGION_ALIASES: dict[str, list[str]] = {
    "nigeria": ["nigeria", "lagos", "abuja", "ibadan", "kano",
                "port harcourt", "ng"],
    "europe": ["france", "paris", "spain", "madrid", "barcelona", "italy",
               "milan", "rome", "ireland", "dublin", "sweden", "stockholm",
               "norway", "oslo", "denmark", "copenhagen", "finland",
               "helsinki", "poland", "warsaw", "austria", "vienna",
               "switzerland", "zurich", "belgium", "brussels", "greece",
               "athens", "czech", "prague", "romania", "bucharest", "hungary",
               "budapest", "turkey", "turkiye", "istanbul", "ankara",
               "lisboa", "fr", "es", "it", "ie", "se", "no", "dk", "fi",
               "pl", "at", "ch", "be", "gr", "cz", "ro", "hu", "hr", "tr",
               "pt", "nl", "de", "gb"],
    "gulf": ["emirates", "dubai", "abu dhabi", "ajman", "sharjah", "saudi",
             "riyadh", "jeddah", "qatar", "doha", "kuwait", "bahrain",
             "oman", "muscat", "ae", "sa", "qa", "kw", "bh", "om"],
    "global": ["remote"],
}

KNOWN_REGIONS = ["nigeria", "europe", "gulf", "global", "nordics", "other"]

_region_keywords: dict[str, list[str]] | None = None


def _name_tokens(text: str) -> list[str]:
    toks = re.findall(r"[a-z]{2,}", text.lower())
    return [t for t in toks if t != "remote"]


def region_keywords() -> dict[str, list[str]]:
    """Merge config.yaml location groupings with REGION_ALIASES (cached)."""
    global _region_keywords
    if _region_keywords is not None:
        return _region_keywords
    merged: dict[str, set[str]] = {r: set(v) for r, v in REGION_ALIASES.items()}
    try:
        from job_ingest.config import load_config
        cfg = load_config(BASE_DIR / "config.yaml")
        for loc in cfg.locations:
            region = (loc.region or "global").lower()
            merged.setdefault(region, set()).update(_name_tokens(loc.name))
            if loc.country_indeed:
                merged[region].update(_name_tokens(loc.country_indeed))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not seed regions from config.yaml: %s", exc)
    _region_keywords = {r: sorted(kws) for r, kws in merged.items()}
    return _region_keywords


def _kw_hit(kw: str, loc: str) -> bool:
    # Short tokens (ISO codes like ng/ae/uk) need word boundaries;
    # longer ones match as substrings ("emirates" in "... Emirates").
    if len(kw) <= 3:
        return re.search(r"\b" + re.escape(kw) + r"\b", loc) is not None
    return kw in loc


def classify_region(location: str | None, is_remote: bool = False) -> str:
    loc = (location or "").lower()
    hits: dict[str, str] = {}  # region -> longest matched keyword
    for region, kws in region_keywords().items():
        best = ""
        for kw in kws:
            if _kw_hit(kw, loc) and len(kw) > len(best):
                best = kw
        if best:
            hits[region] = best
    is_remote_loc = bool(is_remote) or "remote" in loc
    if is_remote_loc:
        concrete = {r: k for r, k in hits.items() if r != "global"}
        if not concrete:
            return "global"
        return max(concrete, key=lambda r: len(concrete[r]))
    if not hits:
        return "other"
    return max(hits, key=lambda r: len(hits[r]))


# --------------------------------------------------------------------------
# DB helpers
# --------------------------------------------------------------------------

def db_path() -> str:
    try:
        from job_ingest.config import load_config
        return str(load_config(BASE_DIR / "config.yaml").db_path)
    except Exception:  # noqa: BLE001
        return str(BASE_DIR / "jobs.db")


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(db_path())
    con.row_factory = sqlite3.Row
    return con


_API_COLS_ENSURED = False


def _ensure_api_cols() -> None:
    """Auto-migrate region/remote_scope columns once per process (same
    pattern as job_ingest/db.py), so explicit-column SELECTs never fail."""
    global _API_COLS_ENSURED
    if _API_COLS_ENSURED:
        return
    try:
        from job_ingest.db import init_db
        init_db(db_path())
    except Exception as exc:  # noqa: BLE001
        log.warning("API column migration failed: %s", exc)
    _API_COLS_ENSURED = True


LIST_COLS = ("rowid AS id", "title", "company", "location", "url",
             "posted_date", "source", "is_remote", "salary_min",
             "salary_max", "match_percentage", "applied", "applied_date",
             "interview_status", "notes", "region", "remote_scope")


class JobUpdate(BaseModel):
    """Partial update for application tracking. applied_date is stamped
    server-side by mark_applied(); never accepted from the client."""
    applied: bool | None = None
    interview_status: str | None = None
    notes: str | None = None


def row_to_job(r: sqlite3.Row, with_description: bool = False) -> dict:
    stored_region = ""
    try:
        stored_region = str(r["region"] or "").strip().lower()
    except (IndexError, KeyError):
        stored_region = ""
    try:
        scope = str(r["remote_scope"] or "").strip().lower() or None
    except (IndexError, KeyError):
        scope = None
    d = {
        "id": r["id"],
        "title": r["title"],
        "company": r["company"],
        "location": r["location"],
        "region": stored_region or classify_region(
            r["location"], bool(r["is_remote"])),
        "remote_scope": scope,
        "url": r["url"],
        "posted_date": r["posted_date"],
        "source": r["source"],
        "is_remote": bool(r["is_remote"]),
        "salary_min": r["salary_min"],
        "salary_max": r["salary_max"],
        "match_percentage": r["match_percentage"],
        "applied": bool(r["applied"]) if "applied" in r.keys() else False,
        "applied_date": r["applied_date"] if "applied_date" in r.keys() else None,
        "interview_status": r["interview_status"] if "interview_status" in r.keys() else None,
        "notes": r["notes"] if "notes" in r.keys() else None,
    }
    if with_description:
        d["description"] = r["description"]
    return d


# --------------------------------------------------------------------------
# Background task tracker (ingest/rescore run long; callers poll /health)
# --------------------------------------------------------------------------

tasks: dict[str, dict] = {
    "ingest": {"state": "idle"},
    "rescore": {"state": "idle"},
}
_tasks_lock = threading.Lock()


def _task_start(name: str) -> bool:
    with _tasks_lock:
        if tasks[name].get("state") == "running":
            return False
        tasks[name] = {"state": "running", "started": time.time()}
        return True


def _task_finish(name: str, ok: bool, detail: str = "") -> None:
    with _tasks_lock:
        tasks[name] = {"state": "done" if ok else "error",
                       "finished": time.time(), "detail": detail}


def _run_ingest() -> None:
    try:
        from ingest import main as ingest_main
        rc = ingest_main(["--config", str(BASE_DIR / "config.yaml"),
                          "--db", db_path()])
        _task_finish("ingest", rc == 0, f"exit code {rc}")
    except Exception as exc:  # noqa: BLE001
        log.exception("Background ingest failed")
        _task_finish("ingest", False, f"{type(exc).__name__}: {exc}")


def _run_rescore(only_unscored: bool) -> None:
    try:
        from job_match.scoring import rescore_all
        stats = rescore_all(db_path(), str(BASE_DIR / "profile.yaml"),
                            only_unscored=only_unscored)
        _task_finish("rescore", True, f"scored {stats['scored']} jobs")
    except Exception as exc:  # noqa: BLE001
        log.exception("Background rescore failed")
        _task_finish("rescore", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------
# Endpoints
# --------------------------------------------------------------------------

@app.get("/jobs")
def list_jobs(
    min_score: float = Query(0.0, ge=0.0, le=100.0),
    source: str | None = None,
    region: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    applied: bool | None = None,
    limit: int = Query(100, ge=1, le=2000),
    offset: int = Query(0, ge=0),
) -> dict:
    clauses: list[str] = []
    params: list = []
    if min_score > 0:
        clauses.append("match_percentage >= ?")
        params.append(min_score)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if applied is not None:
        clauses.append("COALESCE(applied, 0) = ?")
        params.append(1 if applied else 0)
    if date_from:
        clauses.append("posted_date >= ?")
        params.append(date_from)
    if date_to:
        clauses.append("posted_date <= ?")
        params.append(date_to)
    _ensure_api_cols()
    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    q = (f"SELECT {', '.join(LIST_COLS)} FROM jobs {where} "
         "ORDER BY match_percentage DESC NULLS LAST, rowid DESC")
    with _db() as con:
        rows = [dict(r) for r in con.execute(q, params).fetchall()]
    jobs = [row_to_job(r) for r in rows]  # type: ignore[arg-type]
    if region:
        jobs = [j for j in jobs if j["region"] == region.lower()]
    total = len(jobs)
    return {"total": total, "limit": limit, "offset": offset,
            "jobs": jobs[offset:offset + limit]}


@app.get("/jobs/{job_id}")
def job_detail(job_id: int) -> dict:
    _ensure_api_cols()
    with _db() as con:
        r = con.execute(
            f"SELECT {', '.join(LIST_COLS)}, description FROM jobs WHERE rowid = ?",
            (job_id,)).fetchone()
    if r is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    return row_to_job(r, with_description=True)


@app.patch("/jobs/{job_id}")
def update_job(job_id: int, update: JobUpdate) -> dict:
    """Partial update for application tracking (applied / interview_status /
    notes). applied_date is stamped server-side. Returns the updated row."""
    from job_ingest.tracking import (get_job, mark_applied, set_interview_status,
                                     set_notes)
    if get_job(db_path(), job_id) is None:
        raise HTTPException(status_code=404, detail=f"No job with id {job_id}")
    # model_fields_set distinguishes "omitted" from "explicit null" (clear).
    provided = update.model_fields_set
    if "applied" in provided and update.applied is not None:
        mark_applied(db_path(), job_id, update.applied)
    if "interview_status" in provided:
        try:
            set_interview_status(db_path(), job_id, update.interview_status)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
    if "notes" in provided:
        set_notes(db_path(), job_id, update.notes)
    row = get_job(db_path(), job_id)
    assert row is not None
    with _db() as con:
        desc = con.execute("SELECT description FROM jobs WHERE rowid = ?",
                           (job_id,)).fetchone()[0]
    row["description"] = desc
    return row_to_job(row, with_description=True)


@app.get("/filter-options")
def filter_options() -> dict:
    with _db() as con:
        sources = [r[0] for r in
                   con.execute("SELECT DISTINCT source FROM jobs ORDER BY 1")]
    from job_ingest.tracking import VALID_STATUSES
    return {"sources": sources, "regions": KNOWN_REGIONS,
            "interview_statuses": list(VALID_STATUSES)}


@app.get("/health")
def health() -> dict:
    with _db() as con:
        n = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        scored = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE match_percentage IS NOT NULL"
        ).fetchone()[0]
        last = con.execute("SELECT MAX(created_at) FROM jobs").fetchone()[0]
    with _tasks_lock:
        snap = {k: dict(v) for k, v in tasks.items()}
    return {"status": "ok", "rows": n, "scored_rows": scored,
            "last_ingested": last, "tasks": snap}


@app.get("/stats/summary")
def stats_summary() -> dict:
    """Dashboard payload: funnel, status breakdown, timeline, score bins."""
    from datetime import date, datetime, timedelta
    with _db() as con:
        try:
            total_fetched = con.execute(
                "SELECT COALESCE(SUM(total_fetched), 0) FROM ingestion_runs"
            ).fetchone()[0]
        except Exception:  # noqa: BLE001  # table predates migration
            total_fetched = 0
        # NOTE: fetched-vs-kept is not apples-to-apples: fetched sums every
        # run including cross-run duplicates, while kept is the current
        # deduped row count. Directionally useful, not a true conversion rate.
        total_kept = con.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
        applied = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE COALESCE(applied, 0) = 1"
        ).fetchone()[0]
        interview = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE interview_status IS NOT NULL "
            "AND interview_status NOT IN ('pending', 'no_response')"
        ).fetchone()[0]
        offers = con.execute(
            "SELECT COUNT(*) FROM jobs WHERE interview_status = 'offer'"
        ).fetchone()[0]

        breakdown: dict[str, int] = {"not_applied": con.execute(
            "SELECT COUNT(*) FROM jobs WHERE COALESCE(applied, 0) = 0"
        ).fetchone()[0]}
        for status, n in con.execute(
                "SELECT COALESCE(interview_status, 'no_status'), COUNT(*) "
                "FROM jobs WHERE COALESCE(applied, 0) = 1 "
                "GROUP BY interview_status"):
            breakdown[status] = n

        per_day = {d: 0 for d in
                   [(date.today() - timedelta(days=i)).isoformat()
                    for i in range(29, -1, -1)]}
        for day, n in con.execute(
                "SELECT date(applied_date), COUNT(*) FROM jobs "
                "WHERE COALESCE(applied, 0) = 1 AND applied_date IS NOT NULL "
                "AND date(applied_date) >= date('now', '-29 days') "
                "GROUP BY date(applied_date)"):
            if day in per_day:
                per_day[day] = n
        timeline = [{"date": d, "count": per_day[d]} for d in sorted(per_day)]

        bins = [0] * 10
        for (score,) in con.execute(
                "SELECT match_percentage FROM jobs "
                "WHERE match_percentage IS NOT NULL"):
            try:
                bins[min(int(float(score)) // 10, 9)] += 1
            except (TypeError, ValueError):
                continue
        labels = [f"{i * 10}-{i * 10 + 9}" for i in range(9)] + ["90-100"]
        distribution = [{"bin": b, "count": c}
                        for b, c in zip(labels, bins)]

        last_applied = con.execute(
            "SELECT MAX(applied_date) FROM jobs "
            "WHERE COALESCE(applied, 0) = 1").fetchone()[0]
    days_since: int | None = None
    if last_applied:
        try:
            dt = datetime.fromisoformat(str(last_applied).replace("Z", "+00:00"))
            days_since = (datetime.now(dt.tzinfo) - dt).days
        except ValueError:
            days_since = None
    response_rate = round(interview / applied * 100, 1) if applied else 0.0
    return {
        "funnel": {"total_fetched": total_fetched, "total_kept": total_kept,
                   "applied_count": applied, "interview_count": interview,
                   "offer_count": offers},
        "interview_breakdown": breakdown,
        "applications_timeline": timeline,
        "score_distribution": distribution,
        "response_rate": response_rate,
    "days_since_last_application": days_since,
}


@app.post("/ingest", status_code=202)
def trigger_ingest(background: BackgroundTasks) -> dict:
    if not _task_start("ingest"):
        raise HTTPException(status_code=409, detail="Ingest already running")
    background.add_task(_run_ingest)
    return {"status": "ingest started",
            "detail": "Running ingest.main() in background; poll /health"}


@app.post("/rescore", status_code=202)
def trigger_rescore(background: BackgroundTasks,
                    only_unscored: bool = True) -> dict:
    if not _task_start("rescore"):
        raise HTTPException(status_code=409, detail="Rescore already running")
    background.add_task(_run_rescore, only_unscored)
    return {"status": "rescore started",
            "detail": "Running rescore_all() in background; poll /health"}


if __name__ == "__main__":
    import uvicorn
    # Localhost only: single-user local tool, never expose on the network.
    uvicorn.run(app, host="127.0.0.1", port=8000)
