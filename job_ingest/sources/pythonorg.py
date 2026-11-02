"""Python.org Jobs ingestion: GET https://www.python.org/jobs/feed/rss/.

RSS items carry title, link, description and guid, and no pubDate. Title
format is "Role, Company": split on the LAST comma. The first line of the
description is the location (plain text before the HTML body). With no
upstream timestamp, first-seen time (UTC now at fetch) is used as
posted_date, normalized via normalize_timestamp for format consistency.
"""
from __future__ import annotations

import html as _html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests

from ..models import Job
from ..retry import with_retry
from ..timeutil import normalize_timestamp

log = logging.getLogger(__name__)
TIMEOUT = 20
URL = "https://www.python.org/jobs/feed/rss/"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36"}

_STRIP_HTML = re.compile(r"<[^>]+>")


def _clean_html(text: str) -> str:
    text = _STRIP_HTML.sub(" ", _html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _text(item: ET.Element, tag: str) -> str:
    el = item.find(tag)
    return (el.text or "").strip() if el is not None and el.text else ""


@with_retry(max_attempts=3, backoff_seconds=2.0,
            exceptions=(requests.RequestException,),
            source_name="pythonorg")
def _fetch_items() -> list[ET.Element]:
    resp = requests.get(URL, timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    try:
        root = ET.fromstring(resp.content)
    except ET.ParseError as exc:
        raise requests.RequestException(f"Bad RSS XML from {URL}: {exc}")
    return root.findall(".//item")


def _split_title(raw: str) -> tuple[str, str]:
    """'Role, Company' on the LAST comma -> (role, company)."""
    text = _html.unescape(raw or "").strip()
    if "," in text:
        role, company = text.rsplit(",", 1)
        return role.strip(), company.strip()
    return text, ""


def _job_from_item(item: ET.Element, first_seen: str) -> Job | None:
    url = _text(item, "link") or _text(item, "guid")
    if not url:
        return None
    title, company = _split_title(_text(item, "title"))
    if not title:
        return None
    raw_desc = _html.unescape(_text(item, "description"))
    # First line (before any markup) is the location, e.g. "Remote, USA".
    head = re.split(r"<", raw_desc, maxsplit=1)[0]
    location = " ".join(head.split()).strip()
    body = _clean_html(raw_desc)
    if location and body.startswith(location):
        body = body[len(location):].strip()
    job = Job(
        title=title,
        company=company,
        description=body,
        location=location or "Remote",
        url=url,
        posted_date=normalize_timestamp(first_seen),
        source="pythonorg",
        is_remote="remote" in location.lower(),
    )
    return job.clean()


def fetch_pythonorg_jobs() -> list[Job]:
    """Fetch the Python.org jobs RSS feed. Raises on failure."""
    first_seen = datetime.now(timezone.utc).isoformat()
    items = _fetch_items()
    jobs: list[Job] = []
    seen: set[str] = set()
    for item in items:
        try:
            job = _job_from_item(item, first_seen)
            if job is not None and job.url not in seen:
                seen.add(job.url)
                jobs.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed pythonorg item: %s", exc)
    return jobs
