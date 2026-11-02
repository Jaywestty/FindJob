"""NordicTechJobs ingestion via the polite crawler layer.

Discovery: https://nordictechjobs.com/sitemap.xml (a sitemap index) ->
sitemap-jobs.xml. Only individual job URLs of the form /jobs/<id> are kept;
landing pages such as /jobs/english-speaking/sweden are excluded by the
URL pattern.

robots.txt (observed 2026-10-16, also summarized here):
  Allow: /   (plus explicit Allow: /jobs/ and /blog)
  Disallow: /auto-apply/ /recruiters/ /user /admin /auth/
            /resume-builder/tailor /*?page= /*?search=
  Crawl-delay: 1  (honored as max(1, configured default 1.5 s))
Sitemap: https://nordictechjobs.com/sitemap.xml
Because ?page= and ?search= are disallowed, this source NEVER requests a
URL containing those query strings (see _has_banned_query + the test
test_nordictechjobs_no_banned_query_urls).

Detail pages are server-rendered HTML without JSON-LD JobPosting markup
(observed), so the BeautifulSoup fallback (h1 + .job-description) is the
working path; JSON-LD is still tried first via the crawler extractor.

Config (config.yaml ``nordictechjobs`` block): countries list (default
[sweden]) — a job is kept only when its country (JSON-LD addressCountry,
else the city/country line on the page) matches a configured country;
region_tag (default nordics) stored on kept jobs; sitemap_url, max_jobs.
"""
from __future__ import annotations

import logging
import re

from bs4 import BeautifulSoup

from ..crawler import (Crawler, clean_html, existing_urls, extract_fallback,
                       extract_jobposting)
from ..models import Job
from ..timeutil import normalize_timestamp

log = logging.getLogger(__name__)

DEFAULT_SITEMAP = "https://nordictechjobs.com/sitemap.xml"
# Individual job pages only: /jobs/<id> with no further path segments.
JOB_URL_RE = re.compile(r"^https://nordictechjobs\.com/jobs/[^/]+$")
BANNED_QUERY_RE = re.compile(r"[?&](page|search)=", re.IGNORECASE)


def _has_banned_query(url: str) -> bool:
    """True if the URL carries a robots-disallowed ?page= / ?search= query."""
    return bool(BANNED_QUERY_RE.search(url or ""))


def _derive_country(location: str, ld_location: str,
                    countries: list[str]) -> str | None:
    """Match the job's country against the configured countries list.

    The JSON-LD location wins (whole value, then its last comma segment,
    e.g. "Copenhagen, Denmark" -> "denmark"); otherwise whole-word match
    of a configured country name in the page's location line. No
    city->country map lives here: coverage follows the config list.
    Returns None when the job is outside the configured countries.
    """
    wanted = [c.strip().lower() for c in countries if str(c).strip()]
    ld = str(ld_location or "").strip()
    if ld.lower() in wanted:
        return ld.lower()
    if ld:
        tail = ld.split(",")[-1].strip().lower()
        if tail in wanted:
            return tail
    hay = f"{location} {ld}"
    for c in wanted:
        if re.search(r"\b" + re.escape(c) + r"\b", hay, re.IGNORECASE):
            return c
    return None


def _parse_detail(html: str) -> tuple[str, str, str, str]:
    """(title, company, location, description) from a job detail page."""
    ld = extract_jobposting(html)
    if ld and (ld.get("title") or ld.get("description")):
        loc = ld.get("location") or ""
        return (ld.get("title") or "", ld.get("company") or "", loc,
                ld.get("description") or "")
    fb = extract_fallback(html)
    soup = BeautifulSoup(html, "html.parser")
    company, location = "", ""
    h1 = soup.find("h1")
    if h1:
        line = h1.find_next("p")
        if line:
            text = line.get_text(" ", strip=True)
            # Format observed: "Company <sep> City, Country".
            parts = re.split(r"\s*[–—|\-·]\s*", text, maxsplit=1)
            if len(parts) == 2:
                company, location = parts[0].strip(), parts[1].strip()
            else:
                location = text.strip()
    return fb["title"], company, location, fb["description"]


def fetch_nordictechjobs_jobs(db_path: str = "jobs.db",
                              countries: list[str] | None = None,
                              sitemap_url: str = DEFAULT_SITEMAP,
                              max_jobs: int = 100,
                              max_detail_fetches: int = 150,
                              default_delay_seconds: float = 1.5,
                              region_tag: str = "nordics",
                              user_agent: str = "") -> list[Job]:
    """Discover /jobs/<id> URLs from the sitemap and fetch new ones.

    All bounds come from arguments (ingest passes config.yaml values).
    URLs already in jobs.db are skipped before any detail fetch; URLs with
    ?page= / ?search= are never requested (robots.txt).
    """
    countries = [c for c in (countries or ["sweden"]) if str(c).strip()]
    crawler = Crawler(source_name="nordictechjobs",
                      max_detail_fetches=max_detail_fetches,
                      default_delay_seconds=default_delay_seconds,
                      user_agent=user_agent)
    try:
        entries = crawler.read_sitemap(sitemap_url)
    except Exception as exc:  # noqa: BLE001
        log.error("NordicTechJobs sitemap fetch failed: %s", exc)
        raise
    n_banned = sum(1 for u, _ in entries if _has_banned_query(u))
    urls = [(u, lm) for u, lm in entries
            if JOB_URL_RE.match(u) and not _has_banned_query(u)]
    log.info("NordicTechJobs: %d job url(s) discovered "
             "(%d ?page=/?search= url(s) filtered, 0 requested)", len(urls),
             n_banned)
    if not urls:
        return []
    seen = existing_urls(db_path, [u for u, _ in urls])
    fresh = [(u, lm) for u, lm in urls if u not in seen][:max(0, max_jobs)]
    log.info("NordicTechJobs: %d new url(s) to fetch (%d already in DB)",
             len(fresh), len(seen))
    jobs: list[Job] = []
    for url, lastmod in fresh:
        try:
            html = crawler.fetch_detail(url)
        except Exception as exc:  # noqa: BLE001
            log.warning("NordicTechJobs detail fetch failed for %s: %s",
                        url, exc)
            continue
        if not html:
            continue
        try:
            ld = extract_jobposting(html)
            title, company, location, desc = _parse_detail(html)
            if not title or not desc:
                continue
            ld_location = str((ld or {}).get("location") or "")
            country = _derive_country(location, ld_location, countries)
            if country is None:
                continue
            is_remote = bool(ld and ld.get("is_remote")) or bool(
                re.search(r"\bremote\b", f"{title} {location}",
                          re.IGNORECASE))
            job = Job(
                title=title,
                company=company or "NordicTechJobs",
                description=clean_html(desc)[:8000],
                location=location or country.title(),
                url=url,
                posted_date=normalize_timestamp(lastmod),
                source="nordictechjobs",
                region=region_tag,
                is_remote=is_remote,
            )
            jobs.append(job.clean())
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed nordictechjobs page %s: %s",
                        url, exc)
    return jobs
