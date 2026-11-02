"""MyJobMag (Nigeria) ingestion via the polite crawler layer.

Observed robots.txt (2026-10-16, www.myjobmag.com/robots.txt):
  Disallow: /search/jobs?*  /*?  /profile /access /create /saved-jobs
            /signout /activate /login /signup /user/* /videos/tag/*
            /apply-now/ /&page= /job-application/ /learn/
  Sitemap: https://www.myjobmag.com/sitemapindex.xml

Consequences honored here:
- /*? disallows ANY query string, so only plain path URLs are ever
  requested (an explicit "?" guard drops anything else before fetching,
  and the log proves it).
- /search/jobs is never used. Discovery comes from the sitemap index ->
  sitemap-sub-jobs.xml, which lists individual /job/<slug> postings
  (45k, no lastmod). /jobs/<slug> pages are company hubs (multiple roles
  per page), so they are deliberately excluded by the URL pattern.
- With no lastmod and a generalist 45k-URL sitemap, slugs are prefiltered
  by hyphen-token against the configured slug_keywords list (config.yaml)
  before the existing-URL skip and the max_jobs cap.
- Individual /job/ pages carry schema.org JobPosting JSON-LD (title,
  hiringOrganization.name, datePosted, TELECOMMUTE -> is_remote,
  jobLocation address e.g. Lagos). BS fallback when absent.

Config (config.yaml ``myjobmag`` block): sitemap_url (default the
sitemapindex), slug_keywords (default [] = fail-closed: no jobs, with a
warning), max_jobs, region_tag (default nigeria).
"""
from __future__ import annotations

import logging
import re

from ..crawler import (Crawler, clean_html, existing_urls, extract_fallback,
                       extract_jobposting)
from ..models import Job
from ..timeutil import normalize_timestamp

log = logging.getLogger(__name__)

DEFAULT_SITEMAP = "https://www.myjobmag.com/sitemapindex.xml"
# Individual postings only — never company hubs (/jobs/...) or anything
# with a query string (robots /*?).
JOB_URL_RE = re.compile(r"^https://www\.myjobmag\.com/job/[^/?#]+$")


def _slug_tokens(url: str) -> list[str]:
    slug = url.rstrip("/").rsplit("/", 1)[-1].lower()
    return [t for t in re.split(r"[^a-z0-9]+", slug) if t]


def _slug_kept(url: str, keywords: list[str]) -> bool:
    """Hyphen-token match: token equals or starts with a keyword."""
    toks = _slug_tokens(url)
    for kw in keywords:
        kw = str(kw).strip().lower()
        if not kw:
            continue
        for tok in toks:
            if tok == kw or tok.startswith(kw):
                return True
    return False


def fetch_myjobmag_jobs(db_path: str = "jobs.db",
                        sitemap_url: str = DEFAULT_SITEMAP,
                        slug_keywords: list[str] | None = None,
                        max_jobs: int = 60,
                        max_detail_fetches: int = 150,
                        default_delay_seconds: float = 1.5,
                        region_tag: str = "nigeria",
                        user_agent: str = "") -> list[Job]:
    """Discover /job/ URLs from the sitemap; fetch new ones (no queries).

    All bounds come from arguments (ingest passes config.yaml values).
    URLs already in jobs.db are skipped before any detail fetch.
    """
    keywords = [k for k in (slug_keywords or []) if str(k).strip()]
    if not keywords:
        log.warning("MyJobMag: no slug_keywords configured; fetching "
                    "nothing (fail-closed on a 45k-URL generalist sitemap)")
        return []
    crawler = Crawler(source_name="myjobmag",
                      max_detail_fetches=max_detail_fetches,
                      default_delay_seconds=default_delay_seconds,
                      user_agent=user_agent)
    try:
        entries = crawler.read_sitemap(sitemap_url)
    except Exception as exc:  # noqa: BLE001
        log.error("MyJobMag sitemap fetch failed: %s", exc)
        raise
    n_query = sum(1 for u, _ in entries if "?" in u)
    candidates = [u for u, _ in entries
                  if JOB_URL_RE.match(u) and "?" not in u
                  and _slug_kept(u, keywords)]
    log.info("MyJobMag: %d /job/ url(s) match slug keywords "
             "(%d query-string url(s) filtered, 0 requested per robots /*?)",
             len(candidates), n_query)
    if not candidates:
        return []
    seen = existing_urls(db_path, candidates)
    todo = [u for u in candidates
            if u not in seen and "?" not in u][:max(0, max_jobs)]
    log.info("MyJobMag: %d new url(s) to fetch (%d already in DB)",
             len(todo), len(seen))
    jobs: list[Job] = []
    for url in todo:
        if "?" in url:  # robots /*? : never request query-string URLs
            continue
        try:
            html = crawler.fetch_detail(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("MyJobMag detail fetch failed for %s: %s", url, exc)
            continue
        if not html:
            continue
        try:
            ld = extract_jobposting(html)
            if ld and (ld.get("title") or ld.get("description")):
                job = Job(
                    title=ld.get("title") or "",
                    company=ld.get("company") or "MyJobMag",
                    description=(ld.get("description") or "")[:8000],
                    location=ld.get("location") or "",
                    url=url,
                    posted_date=normalize_timestamp(ld.get("date_posted")),
                    source="myjobmag",
                    region=region_tag,
                    is_remote=bool(ld.get("is_remote")),
                    salary_min=ld.get("salary_min"),
                    salary_max=ld.get("salary_max"),
                )
            else:
                fb = extract_fallback(html)
                if not fb["title"]:
                    continue
                job = Job(
                    title=fb["title"],
                    company="MyJobMag",
                    description=clean_html(fb["description"])[:8000],
                    location="",
                    url=url,
                    posted_date=None,
                    source="myjobmag",
                    region=region_tag,
                    is_remote=False,
                )
            if job.title and job.description:
                if "job not found" in job.title.lower():
                    continue  # expired posting still present in the sitemap
                jobs.append(job.clean())
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed myjobmag page %s: %s", url, exc)
    return jobs
