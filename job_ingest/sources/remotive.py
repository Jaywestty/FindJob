"""Remotive ingestion: GET https://remotive.com/api/remote-jobs (public API).

One request per search term; terms are built from the configured
role_categories keywords so coverage follows the filter config.
publication_date arrives as ISO-8601; salary is free text (not parsed).
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
URL = "https://remotive.com/api/remote-jobs"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36"}

_STRIP_HTML = re.compile(r"<[^>]+>")


def _clean_html(text: str) -> str:
    text = _STRIP_HTML.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip()


@with_retry(max_attempts=3, backoff_seconds=2.0,
            exceptions=(requests.RequestException,),
            source_name="remotive")
def _fetch_search(search: str) -> list:
    resp = requests.get(URL, params={"search": search},
                        timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return jobs if isinstance(jobs, list) else []


def _job_from_payload(item: dict) -> Job | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url") or ""
    if not url:
        return None
    loc = str(item.get("candidate_required_location") or "") or "Remote"
    job = Job(
        title=str(item.get("title", "") or ""),
        company=str(item.get("company_name", "") or ""),
        description=_clean_html(str(item.get("description", "") or "")),
        location=loc,
        url=url,
        posted_date=normalize_timestamp(item.get("publication_date")),
        source="remotive",
        is_remote=True,
        salary_min=None,
        salary_max=None,
    )
    return job.clean()


def fetch_remotive_jobs(searches: list[str] | None = None) -> list[Job]:
    """Fetch Remotive per search term, deduped by URL. Raises on failure."""
    searches = [s for s in (searches or []) if s and s.strip()]
    if not searches:
        searches = ["python"]
    jobs: list[Job] = []
    seen: set[str] = set()
    for search in searches:
        try:
            items = _fetch_search(search)
        except Exception as exc:
            log.error("Remotive search %r failed: %s", search, exc)
            raise
        for item in items:
            try:
                job = _job_from_payload(item)
                if job is not None and job.url not in seen:
                    seen.add(job.url)
                    jobs.append(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipping malformed remotive job: %s", exc)
    return jobs
