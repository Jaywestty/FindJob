"""WorkingNomads ingestion: GET https://www.workingnomads.com/api/exposed_jobs/.

A single JSON array (no pagination). Each item carries url, title,
description (HTML), company_name, category_name, tags (comma-separated
string), location, pub_date (ISO-8601 with offset). The url is kept exactly
as given. The location field often states regional restrictions
("Only EU", "US only", ...), so it is preserved unmodified for the
remote-eligibility layer (Part 5). pub_date normalized via
normalize_timestamp.
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
URL = "https://www.workingnomads.com/api/exposed_jobs/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36"}

_STRIP_HTML = re.compile(r"<[^>]+>")


def _clean_html(text: str) -> str:
    import html as _html
    text = _STRIP_HTML.sub(" ", _html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


@with_retry(max_attempts=3, backoff_seconds=2.0,
            exceptions=(requests.RequestException,),
            source_name="workingnomads")
def _fetch() -> list:
    resp = requests.get(URL, timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _job_from_payload(item: dict) -> Job | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    job = Job(
        title=str(item.get("title", "") or ""),
        company=str(item.get("company_name", "") or ""),
        description=_clean_html(str(item.get("description", "") or "")),
        location=str(item.get("location") or "").strip() or "Remote",
        url=url,
        posted_date=normalize_timestamp(item.get("pub_date")),
        source="workingnomads",
        is_remote=True,
        salary_min=None,
        salary_max=None,
    )
    return job.clean()


def fetch_workingnomads_jobs() -> list[Job]:
    """Fetch the exposed-jobs array. Raises on failure."""
    items = _fetch()
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in items:
        try:
            job = _job_from_payload(item)
            if job is not None and job.url not in seen:
                seen.add(job.url)
                jobs.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed workingnomads job: %s", exc)
    return jobs
