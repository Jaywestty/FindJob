"""Jobicy ingestion: GET https://jobicy.com/api/v2/remote-jobs (public API).

Fetched 50 per request (count=50) with industry=engineering, which the API
accepts directly (verified: echoed back in appliedFilters). The API exposes
no page/offset params (probed: page/paged/offset all return HTTP 400), and
the `level` param was probed with entry-level/junior/mid-level/senior and
rejected for all of them (success=False) — so seniority is left to
job_passes_filters instead of params the API does not support.
pubDate is ISO-8601.
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
URL = "https://jobicy.com/api/v2/remote-jobs"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36"}
PAGE_SIZE = 50

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
            source_name="jobicy")
def _fetch_page() -> list:
    resp = requests.get(URL, params={"count": PAGE_SIZE,
                                     "industry": "engineering"},
                        timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict) or not data.get("success", True):
        raise requests.RequestException(
            f"Jobicy error response: {str(data)[:200]}")
    jobs = data.get("jobs", [])
    return jobs if isinstance(jobs, list) else []


def _job_from_payload(item: dict) -> Job | None:
    if not isinstance(item, dict):
        return None
    url = item.get("url") or ""
    if not url:
        return None
    job = Job(
        title=str(item.get("jobTitle", "") or ""),
        company=str(item.get("companyName", "") or ""),
        description=_clean_html(str(item.get("jobDescription", "") or "")),
        location=str(item.get("jobGeo", "") or "") or "Remote",
        url=url,
        posted_date=normalize_timestamp(item.get("pubDate")),
        source="jobicy",
        is_remote=True,
        salary_min=_to_float(item.get("salaryMin")),
        salary_max=_to_float(item.get("salaryMax")),
    )
    return job.clean()


def fetch_jobicy_jobs() -> list[Job]:
    """Fetch one page (50) of engineering postings. Raises on failure."""
    try:
        items = _fetch_page()
    except Exception as exc:
        log.error("Jobicy fetch failed: %s", exc)
        raise
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in items:
        try:
            job = _job_from_payload(item)
            if job is not None and job.url not in seen:
                seen.add(job.url)
                jobs.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed jobicy job: %s", exc)
    return jobs
