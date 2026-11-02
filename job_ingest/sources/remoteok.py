"""RemoteOK ingestion: GET https://remoteok.com/api (official public API).

Response is a JSON array whose FIRST element is metadata, not a job —
it is skipped. Timestamps come as epoch seconds + ISO strings.
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
URL = "https://remoteok.com/api"
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
            source_name="remoteok")
def _fetch() -> list:
    resp = requests.get(URL, timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _job_from_payload(item: dict) -> Job | None:
    if not isinstance(item, dict) or "position" not in item:
        return None  # metadata element or unexpected shape
    url = item.get("apply_url") or item.get("url") or ""
    if not url:
        return None
    loc = str(item.get("location") or "") or "Remote"
    job = Job(
        title=str(item.get("position", "") or ""),
        company=str(item.get("company", "") or ""),
        description=_clean_html(str(item.get("description", "") or "")),
        location=loc,
        url=url,
        posted_date=normalize_timestamp(item.get("epoch") or item.get("date")),
        source="remoteok",
        is_remote=True,
        salary_min=_to_float(item.get("salary_min")),
        salary_max=_to_float(item.get("salary_max")),
    )
    return job.clean()


def fetch_remoteok_jobs() -> list[Job]:
    """Fetch the RemoteOK board. Raises on failure."""
    items = _fetch()
    jobs: list[Job] = []
    for item in items[1:]:  # [0] is metadata, not a job
        try:
            job = _job_from_payload(item)
            if job is not None:
                jobs.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed remoteok job: %s", exc)
    return jobs
