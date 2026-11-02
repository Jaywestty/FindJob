"""WeWorkRemotely ingestion: public RSS feeds (no HTML scraping).

Feeds: the main remote-jobs feed plus the programming category feed.
WWR returns 403 to obviously-automated user agents even on public RSS, so
every request carries a real browser-like User-Agent, and any non-200
response is logged loudly (a silent 403 would lose the whole source with
no error surfacing). pubDate is RFC-822, normalized via normalize_timestamp.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET

import requests

from ..models import Job
from ..retry import with_retry
from ..timeutil import normalize_timestamp

log = logging.getLogger(__name__)
TIMEOUT = 20
FEEDS = [
    "https://weworkremotely.com/remote-jobs.rss",
    "https://weworkremotely.com/categories/remote-programming-jobs.rss",
]
# Browser-like UA is required: WWR 403s automated user agents on RSS too.
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36"}
DC_CREATOR = "{http://purl.org/dc/elements/1.1/}creator"

_STRIP_HTML = re.compile(r"<[^>]+>")


def _clean_html(text: str) -> str:
    # RSS descriptions arrive entity-escaped; unescape after tag stripping.
    import html as _html
    text = _STRIP_HTML.sub(" ", _html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _text(item: ET.Element, tag: str) -> str:
    el = item.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


@with_retry(max_attempts=3, backoff_seconds=2.0,
            exceptions=(requests.RequestException,),
            source_name="weworkremotely")
def _fetch_feed(url: str) -> list[ET.Element]:
    resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    if resp.status_code != 200:
        log.error("WeWorkRemotely feed %s returned HTTP %d "
                  "(check User-Agent / blocking)", url, resp.status_code)
        resp.raise_for_status()
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise requests.RequestException(f"Bad RSS XML from {url}: {exc}")
    return root.findall(".//item")


def _job_from_item(item: ET.Element) -> Job | None:
    url = _text(item, "link") or _text(item, "guid")
    if not url:
        return None
    company = _text(item, DC_CREATOR)
    title = _text(item, "title")
    if not company and " : " in title:  # "Company : Title" convention
        company, title = (p.strip() for p in title.split(" : ", 1))
    region = _text(item, "region")
    job = Job(
        title=title,
        company=company,
        description=_clean_html(_text(item, "description")),
        location=region or "Remote",
        url=url,
        posted_date=normalize_timestamp(_text(item, "pubDate") or None),
        source="weworkremotely",
        is_remote=True,
        salary_min=None,
        salary_max=None,
    )
    return job.clean()


def fetch_wwr_jobs() -> list[Job]:
    """Fetch main + programming feeds, deduped by link. Raises on failure."""
    jobs: list[Job] = []
    seen: set[str] = set()
    for feed in FEEDS:
        try:
            items = _fetch_feed(feed)
        except Exception as exc:
            log.error("WeWorkRemotely feed %s failed: %s", feed, exc)
            raise
        for item in items:
            try:
                job = _job_from_item(item)
                if job is not None and job.url not in seen:
                    seen.add(job.url)
                    jobs.append(job)
            except Exception as exc:  # noqa: BLE001
                log.warning("Skipping malformed wwr item: %s", exc)
    return jobs
