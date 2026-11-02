"""Ashby ingestion: GET https://api.ashbyhq.com/posting-api/job-board/<slug>.

One response carries the whole board (no pagination): {"jobs": [...]},
observed live 2026-10-16. Field mapping from a real response:
  title            -> Job.title
  jobUrl           -> Job.url
  descriptionHtml  -> Job.description (HTML stripped)
  location (+ secondaryLocations names, address.postalAddress.addressCountry)
                   -> Job.location
  isRemote / workplaceType == Remote -> Job.is_remote
  publishedAt      -> Job.posted_date (normalize_timestamp)
  compensation.compensationTiers [{min,max}] or the
    compensationTierSummary / scrapeableCompensationSalarySummary strings
                   -> salary_min/max where parsable
Slugs are case-sensitive and come from config (ashby_companies); the board
name (posting company) is not echoed in the payload, so the configured slug
is stored as the company. Dead slugs (HTTP 404) are dropped by the startup
slug check before this module is ever called.
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
BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36"}

_STRIP_HTML = re.compile(r"<[^>]+>")
_RANGE_RE = re.compile(r"\$?\s*(\d[\d,]*)\s*(?:-|–|—|to)\s*\$?\s*(\d[\d,]*)",
                       re.IGNORECASE)
_SINGLE_RE = re.compile(r"\$?\s*(\d[\d,]{3,})")


def _clean_html(text: str) -> str:
    import html as _html
    text = _STRIP_HTML.sub(" ", _html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _num(raw: str) -> float | None:
    try:
        f = float(raw.replace(",", ""))
        return f if f > 0 else None
    except (TypeError, ValueError):
        return None


def _salary_from_summary(text: str) -> tuple[float | None, float | None]:
    m = _RANGE_RE.search(text or "")
    if m:
        return _num(m.group(1)), _num(m.group(2))
    m = _SINGLE_RE.search(text or "")
    if m:
        v = _num(m.group(1))
        return v, v
    return None, None


def _salary_from_comp(comp: dict) -> tuple[float | None, float | None]:
    if not isinstance(comp, dict):
        return None, None
    tiers = comp.get("compensationTiers") or []
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            lo = _num(tier.get("min") or tier.get("minValue") or "")
            hi = _num(tier.get("max") or tier.get("maxValue") or "")
            if lo is not None or hi is not None:
                return lo, (hi if hi is not None else lo)
    for key in ("scrapeableCompensationSalarySummary",
                "compensationTierSummary"):
        text = comp.get(key)
        if text:
            lo, hi = _salary_from_summary(str(text))
            if lo is not None or hi is not None:
                return lo, hi
    return None, None


@with_retry(max_attempts=3, backoff_seconds=2.0,
            exceptions=(requests.RequestException,),
            source_name="ashby")
def _fetch_board(slug: str) -> list:
    url = BOARD_URL.format(slug=slug)
    resp = requests.get(url, params={"includeCompensation": "true"},
                        timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    jobs = data.get("jobs", []) if isinstance(data, dict) else []
    return jobs if isinstance(jobs, list) else []


def _location_from_item(item: dict) -> str:
    loc = str(item.get("location") or "").strip()
    extra: list[str] = []
    for sec in item.get("secondaryLocations") or []:
        if isinstance(sec, str) and sec.strip():
            extra.append(sec.strip())
    country = ""
    try:
        addr = (item.get("address") or {}).get("postalAddress") or {}
        country = str(addr.get("addressCountry") or "").strip()
    except AttributeError:
        country = ""
    parts = [p for p in [loc, *extra] if p]
    if country and country.lower() not in " ".join(parts).lower():
        parts.append(country)
    return ", ".join(parts)


def _job_from_payload(slug: str, item: dict) -> Job | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("jobUrl") or "").strip()
    if not url:
        return None
    title = str(item.get("title", "") or "").strip()
    if not title:
        return None
    desc = _clean_html(str(item.get("descriptionHtml", "") or ""))
    if not desc:
        desc = _clean_html(str(item.get("descriptionPlain", "") or ""))
    loc = _location_from_item(item)
    is_remote = bool(item.get("isRemote")) or "remote" in str(
        item.get("workplaceType", "") or "").lower() \
        or "remote" in loc.lower() or "remote" in str(
        item.get("location", "") or "").lower()
    if not loc:
        loc = "Remote" if is_remote else ""
    smin, smax = _salary_from_comp(item.get("compensation") or {})
    job = Job(
        title=title,
        company=slug,
        description=desc[:12000],
        location=loc,
        url=url,
        posted_date=normalize_timestamp(item.get("publishedAt")),
        source=f"ashby:{slug}",
        is_remote=is_remote,
        salary_min=smin,
        salary_max=smax,
    )
    return job.clean()


def fetch_ashby_jobs(slug: str) -> list[Job]:
    """Fetch one Ashby board (whole board, one response). Raises on failure."""
    items = _fetch_board(slug)
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in items:
        try:
            job = _job_from_payload(slug, item)
            if job is not None and job.url not in seen:
                seen.add(job.url)
                jobs.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed ashby job for %s: %s", slug, exc)
    return jobs
