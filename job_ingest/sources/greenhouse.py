"""Greenhouse ATS ingestion: https://boards-api.greenhouse.io/v1/boards/{company}/jobs"""
from __future__ import annotations

import logging
import re

import requests

from ..models import Job, parse_posted_date
from ..retry import with_retry

log = logging.getLogger(__name__)
TIMEOUT = 20

_STRIP_HTML = re.compile(r"<[^>]+>")


def _clean_html(text: str) -> str:
    text = _STRIP_HTML.sub(" ", text or "")
    return re.sub(r"\s+", " ", text).strip()


@with_retry(max_attempts=3, backoff_seconds=2.0,
            exceptions=(requests.RequestException,),
            source_name="greenhouse")
def _fetch_board(company: str) -> dict:
    url = f"https://boards-api.greenhouse.io/v1/boards/{company}/jobs?content=true"
    resp = requests.get(url, timeout=TIMEOUT,
                        headers={"User-Agent": "jobtool/1.0"})
    resp.raise_for_status()
    return resp.json()


def _job_from_payload(company: str, item: dict) -> Job | None:
    url = item.get("absolute_url") or ""
    if not url:
        return None
    loc = ""
    location_obj = item.get("location") or {}
    if isinstance(location_obj, dict):
        loc = str(location_obj.get("name", "") or "")
    offices = item.get("offices") or []
    if not loc and offices and isinstance(offices, list):
        loc = str(offices[0].get("name", "") or "")
    is_remote = "remote" in loc.lower()
    job = Job(
        title=str(item.get("title", "") or ""),
        company=company,
        description=_clean_html(str(item.get("content", "") or "")),
        location=loc,
        url=url,
        posted_date=parse_posted_date(item.get("updated_at")),
        source=f"greenhouse:{company}",
        is_remote=is_remote,
        salary_min=None,
        salary_max=None,
    )
    return job.clean()


def fetch_greenhouse_jobs(company: str) -> list[Job]:
    """Fetch all postings for one Greenhouse board. Raises on failure."""
    data = _fetch_board(company)
    items = data.get("jobs", []) if isinstance(data, dict) else []
    jobs: list[Job] = []
    for item in items:
        try:
            job = _job_from_payload(company, item)
            if job is not None:
                jobs.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed greenhouse job for %s: %s", company, exc)
    return jobs
