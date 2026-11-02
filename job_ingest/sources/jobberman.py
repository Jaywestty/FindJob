"""Jobberman (Nigeria) ingestion via the polite crawler layer.

Observed robots.txt (2026-10-16, www.jobberman.com/robots.txt):
  Disallow: /admin/ /account/ /cdn-cgi/ /api/ /job/ /blog /forms
            /subscribe /csrf /confirm-email /ajax/ /report-job/
            /*q=* /*industry=* /*sort=* /*salary_ids=* /*company_name=*
            /*experience=* /*keywords=* /*job_type=* /*page=* /discover/page
  Allow (explicit, pages 2-10 only): *page=2$ .. *page=10$ (and & variants),
            /account/login
  Sitemaps: sitemap-category-en.xml, sitemap-directory-en.xml,
            sitemap-listings-index-en.xml, discover/sitemap_index.xml

Consequences honored here:
- The real individual listing pattern (found by inspecting the sitemap +
  the category page https://www.jobberman.com/jobs/software-data) is
  /listings/<slug> — NOT /job/ (disallowed). Only /listings/ URLs are
  ever fetched, and every fetch goes through the robots helper; if
  /listings/ ever becomes disallowed the source disables itself for the
  run with a clear log line (the documented alternative — category-card
  data only — is unnecessary while listings stay allowed).
- No filter query parameters are ever used (q, industry, sort, salary_ids,
  company_name, experience, keywords, job_type) and no pagination past
  page 10 is needed: discovery comes from the per-category listing
  sitemaps (sitemap-listings-<category>-en.xml, with lastmod), not from
  paged category HTML.
- Detail pages carry full schema.org JobPosting JSON-LD (title,
  description HTML, datePosted, employmentType, baseSalary NGN MONTH,
  jobLocationType TELECOMMUTE -> is_remote). hiringOrganization is an
  @id reference without a name, so the company/location fall back to the
  page-embedded employer_name / location_name fields, then to BS parsing.

Config (config.yaml ``jobberman`` block): categories (sitemap names,
default [software-data]), days (lastmod freshness, default 30),
max_jobs, region_tag (default nigeria).
"""
from __future__ import annotations

import logging
import re

from ..crawler import (Crawler, clean_html, existing_urls, extract_fallback,
                       extract_jobposting)
from ..models import Job
from ..timeutil import normalize_timestamp

log = logging.getLogger(__name__)

LISTINGS_RE = re.compile(r"^https://www\.jobberman\.com/listings/[^/]+$")
SITEMAP_URL = ("https://www.jobberman.com/sitemap-listings-{category}-en.xml")
# Embedded page data (structural fields, not keywords).
_EMPLOYER_RE = re.compile(r'"employer_name"\s*:\s*"([^"]{1,160})"')
_LOCATION_RE = re.compile(r'"location_name"\s*:\s*"([^"]{1,160})"')
_QUAL_RE = re.compile(r'"qualifications"\s*:\s*"([^"]{1,80})"')


def _page_company_location(html: str) -> tuple[str, str, str]:
    m = _EMPLOYER_RE.search(html)
    company = m.group(1).strip() if m else ""
    m = _LOCATION_RE.search(html)
    location = m.group(1).strip() if m else ""
    m = _QUAL_RE.search(html)
    seniority = m.group(1).strip() if m else ""
    return company, location, seniority


def fetch_jobberman_jobs(db_path: str = "jobs.db",
                         categories: list[str] | None = None,
                         days: int | None = 30,
                         max_jobs: int = 60,
                         max_detail_fetches: int = 150,
                         default_delay_seconds: float = 1.5,
                         region_tag: str = "nigeria",
                         user_agent: str = "") -> list[Job]:
    """Discover /listings/ URLs from category sitemaps; fetch new ones.

    All bounds come from arguments (ingest passes config.yaml values).
    URLs already in jobs.db are skipped before any detail fetch.
    """
    categories = [c for c in (categories or ["software-data"])
                  if str(c).strip()]
    crawler = Crawler(source_name="jobberman",
                      max_detail_fetches=max_detail_fetches,
                      default_delay_seconds=default_delay_seconds,
                      user_agent=user_agent)
    # Runtime robots gate: /listings/ must be allowed, else disable for run.
    probe = "https://www.jobberman.com/listings/_robots-probe"
    if not crawler.allowed(probe):
        log.error("Jobberman: /listings/ disallowed by robots.txt; "
                  "disabling source for this run")
        return []
    discovered: list[tuple[str, str | None]] = []
    for cat in categories:
        try:
            entries = crawler.read_sitemap(SITEMAP_URL.format(category=cat),
                                           days=days)
        except Exception as exc:  # noqa: BLE001
            log.error("Jobberman sitemap %r failed: %s", cat, exc)
            raise
        fresh = [(u, lm) for u, lm in entries if LISTINGS_RE.match(u)]
        log.info("Jobberman: category %r -> %d listing url(s)", cat,
                 len(fresh))
        discovered.extend(fresh)
    if not discovered:
        return []
    seen = existing_urls(db_path, [u for u, _ in discovered])
    todo = [(u, lm) for u, lm in discovered
            if u not in seen][:max(0, max_jobs)]
    log.info("Jobberman: %d new url(s) to fetch (%d already in DB)",
             len(todo), len(seen))
    jobs: list[Job] = []
    for url, lastmod in todo:
        try:
            html = crawler.fetch_detail(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("Jobberman detail fetch failed for %s: %s", url, exc)
            continue
        if not html:
            continue
        try:
            ld = extract_jobposting(html)
            company, loc_name, seniority = _page_company_location(html)
            if ld and (ld.get("title") or ld.get("description")):
                location = ld.get("location") or loc_name
                job = Job(
                    title=ld.get("title") or "",
                    company=ld.get("company") or company or "Jobberman",
                    description=(ld.get("description") or "")[:8000],
                    location=location,
                    url=url,
                    posted_date=normalize_timestamp(ld.get("date_posted")
                                                    or lastmod),
                    source="jobberman",
                    region=region_tag,
                    is_remote=bool(ld.get("is_remote")),
                    salary_min=ld.get("salary_min"),
                    salary_max=ld.get("salary_max"),
                    seniority=seniority or None,
                )
            else:
                fb = extract_fallback(html)
                if not fb["title"]:
                    continue
                job = Job(
                    title=fb["title"],
                    company=company or "Jobberman",
                    description=clean_html(fb["description"])[:8000],
                    location=loc_name,
                    url=url,
                    posted_date=normalize_timestamp(lastmod),
                    source="jobberman",
                    region=region_tag,
                    is_remote=bool(re.search(
                        r"\b(remote|work from home)\b", loc_name,
                        re.IGNORECASE)),
                    seniority=seniority or None,
                )
            if job.title and job.description:
                jobs.append(job.clean())
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed jobberman page %s: %s", url, exc)
    return jobs
