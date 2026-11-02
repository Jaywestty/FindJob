"""EnglishJobs.se ingestion (Sweden-focused English-language board).

What was found (probed 2026-10-16):
- wp-sitemap.xml is a sitemap index exposing, among others,
  wp-sitemap-posts-jobs-1.xml with ~259 individual job URLs of the form
  /jobs/english-jobs-sweden/<slug>.
- The WordPress REST API exposes the same postings as a custom post type:
  GET /wp-json/wp/v2/jobs returns paged JSON (X-WP-Total ~259) with id,
  link, title.rendered, content.rendered, date and taxonomy ids inline.
- The HTML listing at /all-jobs/ exists but would require parsing listing
  HTML plus one detail fetch per job.

Choice: the REST API. It is the most stable option (structured JSON, no
listing-HTML parsing, dates and full content inline, no per-job detail
fetches), preferred over sitemap+crawl and over /all-jobs/ scraping per
the "sitemap or REST over listing HTML" rule. robots.txt only disallows
/wp-admin/ (plus admin-ajax allow), so /wp-json/ is permitted.

Config (config.yaml ``englishjobs`` block): countries (default [sweden]),
default_location, per_page, max_pages, region_tag (default nordics).
"""
from __future__ import annotations

import logging
import re

import requests

from ..crawler import Crawler, clean_html, existing_urls
from ..models import Job
from ..retry import with_retry
from ..timeutil import normalize_timestamp

log = logging.getLogger(__name__)

BASE = "https://www.englishjobs.se"
REST_URL = f"{BASE}/wp-json/wp/v2/jobs"
TIMEOUT = 20

_COMPANY_RE = re.compile(r"(?:company|employer)\s*:\s*([^\n<]{2,120})",
                         re.IGNORECASE)


def _strip_wp_html(rendered: str) -> str:
    return clean_html(str(rendered or ""))


@with_retry(max_attempts=3, backoff_seconds=2.0,
            exceptions=(requests.RequestException,),
            source_name="englishjobs")
def _fetch_page(crawler: Crawler, page: int, per_page: int) -> tuple[list, int]:
    params = {"per_page": per_page, "page": page, "_fields": "id,link,title,"
              "content,date,modified,slug"}
    resp = requests.get(REST_URL, params=params, timeout=TIMEOUT,
                        headers=crawler.headers)
    resp.raise_for_status()
    total_pages = 1
    try:
        total_pages = int(resp.headers.get("X-WP-TotalPages", "1") or 1)
    except ValueError:
        total_pages = 1
    data = resp.json()
    return data if isinstance(data, list) else [], total_pages


def _job_from_item(item: dict, default_location: str,
                   region_tag: str) -> Job | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("link") or "").strip()
    if not url:
        return None
    title = _strip_wp_html((item.get("title") or {}).get("rendered", "")
                           if isinstance(item.get("title"), dict)
                           else str(item.get("title") or ""))
    content = _strip_wp_html((item.get("content") or {}).get("rendered", "")
                             if isinstance(item.get("content"), dict)
                             else str(item.get("content") or ""))
    if not title:
        return None
    company = ""
    m = _COMPANY_RE.search(content)
    if m:
        company = m.group(1).strip()
    is_remote = bool(re.search(r"\b(remote|work from home|work-from-home)\b",
                               f"{title} {content[:500]}", re.IGNORECASE))
    job = Job(
        title=title,
        company=company or "EnglishJobs",
        description=content[:8000],
        location=default_location,
        url=url,
        posted_date=normalize_timestamp(item.get("date")),
        source="englishjobs",
        region=region_tag,
        is_remote=is_remote,
    )
    return job.clean()


def fetch_englishjobs_jobs(db_path: str = "jobs.db",
                           countries: list[str] | None = None,
                           default_location: str = "Sweden",
                           per_page: int = 100,
                           max_pages: int = 5,
                           max_detail_fetches: int = 150,
                           default_delay_seconds: float = 1.5,
                           region_tag: str = "nordics",
                           user_agent: str = "") -> list[Job]:
    """Page the WP REST jobs endpoint; skip URLs already in jobs.db.

    All bounds come from arguments (ingest passes config.yaml values).
    The countries list is accepted for interface parity (the board is
    Sweden-only; default_location carries the country).
    """
    countries = [c for c in (countries or ["sweden"]) if str(c).strip()]
    crawler = Crawler(source_name="englishjobs",
                      max_detail_fetches=max_detail_fetches,
                      default_delay_seconds=default_delay_seconds,
                      user_agent=user_agent)
    jobs: list[Job] = []
    seen_urls: set[str] = set()
    total_pages = 1
    for page in range(1, max(1, max_pages) + 1):
        try:
            crawler._polite_wait(REST_URL)
            items, total_pages = _fetch_page(crawler, page, per_page)
        except Exception as exc:  # noqa: BLE001
            log.error("EnglishJobs REST page %d failed: %s", page, exc)
            raise
        if not items:
            break
        links = [str(it.get("link") or "") for it in items]
        already = existing_urls(db_path, [u for u in links if u])
        for item in items:
            url = str(item.get("link") or "")
            if not url or url in already or url in seen_urls:
                continue
            seen_urls.add(url)
            try:
                job = _job_from_item(item, default_location, region_tag)
                if job is not None:
                    jobs.append(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipping malformed englishjobs item: %s", exc)
        if page >= total_pages:
            break
    log.info("EnglishJobs: %d posting(s) collected", len(jobs))
    return jobs
