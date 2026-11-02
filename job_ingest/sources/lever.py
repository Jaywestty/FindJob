"""Lever ATS ingestion: https://api.lever.co/v0/postings/{company}"""
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
            source_name="lever")
def _fetch_postings(company: str) -> list:
    url = f"https://api.lever.co/v0/postings/{company}"
    resp = requests.get(url, timeout=TIMEOUT,
                        headers={"User-Agent": "jobtool/1.0"})
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def _job_from_payload(company: str, item: dict) -> Job | None:
    url = item.get("hostedUrl") or item.get("applyUrl") or ""
    if not url:
        return None
    cats = item.get("categories") or {}
    loc = str(cats.get("location", "") or "")
    workplace = str(item.get("workplaceType", "") or cats.get("workplaceType", "") or "")
    is_remote = "remote" in (loc + " " + workplace).lower()
    desc = _clean_html(str(item.get("description", "") or ""))
    lists = item.get("lists") or []
    if lists and not desc:
        try:
            desc = _clean_html(" ".join(
                str(x.get("content", "")) for x in lists if isinstance(x, dict)))
        except Exception:  # noqa: BLE001
            pass
    job = Job(
        title=str(item.get("text", "") or ""),
        company=company,
        description=desc,
        location=loc,
        url=url,
        posted_date=parse_posted_date(item.get("createdAt") or item.get("datePosted")),
        source=f"lever:{company}",
        is_remote=is_remote,
        salary_min=None,
        salary_max=None,
    )
    return job.clean()


def fetch_lever_jobs(company: str) -> list[Job]:
    """Fetch all postings for one Lever company. Raises on failure."""
    items = _fetch_postings(company)
    jobs: list[Job] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        try:
            job = _job_from_payload(company, item)
            if job is not None:
                jobs.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed lever job for %s: %s", company, exc)
    return jobs
