"""Himalayas ingestion: GET https://himalayas.app/jobs/api (public API).

Cursor-paged: each response carries nextCursor, passed back as ?cursor=.
Seen cursor values are tracked and paging stops if a cursor repeats
(guards against an infinite loop); max_pages bounds the run regardless.
pubDate arrives as epoch seconds. The native `seniority` list is preserved
onto Job.seniority instead of relying only on regex for this source.
"""
from __future__ import annotations

import logging
import re

import requests

from ..models import Job
from ..retry import with_retry
from ..timeutil import normalize_timestamp

log = logging.getLogger(__name__)
TIMEOUT = 20
URL = "https://himalayas.app/jobs/api"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36"}

_STRIP_HTML = re.compile(r"<[^>]+>")


def _clean_html(text: str) -> str:
    text = _STRIP_HTML.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _to_float(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


@with_retry(max_attempts=3, backoff_seconds=2.0,
            exceptions=(requests.RequestException,),
            source_name="himalayas")
def _fetch_page(cursor: str | None) -> dict:
    params = {"cursor": cursor} if cursor else {}
    resp = requests.get(URL, params=params, timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _job_from_payload(item: dict) -> Job | None:
    if not isinstance(item, dict):
        return None
    url = item.get("applicationLink") or item.get("guid") or ""
    if not url:
        return None
    restrictions = item.get("locationRestrictions") or []
    loc = ", ".join(str(x) for x in restrictions if x) or "Remote"
    seniority = item.get("seniority") or []
    seniority_str = ", ".join(str(x) for x in seniority if x) or None
    job = Job(
        title=str(item.get("title", "") or ""),
        company=str(item.get("companyName", "") or ""),
        description=_clean_html(str(item.get("description", "") or "")),
        location=loc,
        url=url,
        posted_date=normalize_timestamp(item.get("pubDate")),
        source="himalayas",
        is_remote=True,
        salary_min=_to_float(item.get("minSalary")),
        salary_max=_to_float(item.get("maxSalary")),
        seniority=seniority_str,
    )
    return job.clean()


def fetch_himalayas_jobs(max_pages: int = 15) -> list[Job]:
    """Walk cursor pages (20 jobs each). Fifteen pages by default: the feed
    is generalist and strict role filtering keeps only a fraction, so a
    deeper walk is needed for a meaningful yield. Raises on failure."""
    jobs: list[Job] = []
    seen_cursors: set[str] = set()
    cursor: str | None = None
    for _ in range(max(1, max_pages)):
        try:
            page = _fetch_page(cursor)
        except Exception as exc:
            log.error("Himalayas page (cursor=%r) failed: %s", cursor, exc)
            raise
        for item in page.get("jobs", []) or []:
            try:
                job = _job_from_payload(item)
                if job is not None:
                    jobs.append(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipping malformed himalayas job: %s", exc)
        nxt = page.get("nextCursor")
        if not nxt or nxt in seen_cursors:
            if nxt:
                log.warning("Himalayas cursor repeated; stopping pagination")
            break
        seen_cursors.add(nxt)
        cursor = nxt
    return jobs
