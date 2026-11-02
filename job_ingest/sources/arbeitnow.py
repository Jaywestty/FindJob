"""Arbeitnow ingestion: GET https://www.arbeitnow.com/api/job-board-api.

Paginated JSON: {"data": [...], "links": {"next": <url>|null}, "meta": ...}.
Each item carries slug, company_name, title, description (HTML), remote
(bool), url, tags, job_types, location, created_at (epoch seconds).
Pagination follows links.next up to max_pages and stops if a link repeats
(guard against infinite loops). created_at is epoch seconds, normalized
via normalize_timestamp. Options (config extra_sources values): max_pages.
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
URL = "https://www.arbeitnow.com/api/job-board-api"
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
            source_name="arbeitnow")
def _fetch_page(url: str) -> dict:
    resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _job_from_payload(item: dict) -> Job | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url:
        return None
    remote = bool(item.get("remote"))
    loc = str(item.get("location") or "").strip()
    if not loc:
        loc = "Remote" if remote else ""
    job = Job(
        title=str(item.get("title", "") or ""),
        company=str(item.get("company_name", "") or ""),
        description=_clean_html(str(item.get("description", "") or "")),
        location=loc,
        url=url,
        posted_date=normalize_timestamp(item.get("created_at")),
        source="arbeitnow",
        is_remote=remote,
        salary_min=None,
        salary_max=None,
    )
    return job.clean()


def fetch_arbeitnow_jobs(max_pages: int = 5) -> list[Job]:
    """Walk links.next pages (300 jobs each). Raises on failure."""
    jobs: list[Job] = []
    seen_urls: set[str] = set()
    seen_links: set[str] = set()
    url: str | None = URL
    for _ in range(max(1, max_pages)):
        if not url or url in seen_links:
            if url:
                log.warning("Arbeitnow pagination link repeated; stopping")
            break
        seen_links.add(url)
        try:
            page = _fetch_page(url)
        except Exception as exc:
            log.error("Arbeitnow page %r failed: %s", url, exc)
            raise
        for item in page.get("data", []) or []:
            try:
                job = _job_from_payload(item)
                if job is not None and job.url not in seen_urls:
                    seen_urls.add(job.url)
                    jobs.append(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipping malformed arbeitnow job: %s", exc)
        links = page.get("links") or {}
        url = (links.get("next") or None) if isinstance(links, dict) else None
    return jobs
