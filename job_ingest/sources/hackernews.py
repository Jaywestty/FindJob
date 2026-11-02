"""Hacker News "Who is hiring?" ingestion via the public Algolia API.

Thread discovery (no hardcoded thread id): the latest story tagged
story+author_whoishiring whose title starts with "Ask HN: Who is hiring?"
is used. "Who wants to be hired" and freelancer threads are ignored, and
only threads newer than months_back (default 2) are considered.

Comments come from /items/<story id> (verified to contain the children
tree); if it ever does not, search_by_date with tags=comment,story_<id> is
paged as a fallback. Only DIRECT children of the story are postings;
deleted/dead comments are skipped. The first line is usually
"Company | Role | Location | REMOTE": split on "|", falling back to the
first 120 characters for the title. The posting url is the comment link
https://news.ycombinator.com/item?id=<comment id> and posted_date is the
comment's created_at. The description stored (and hence filtered) is the
first line plus the first 400 characters of the body.
"""
from __future__ import annotations

import html as _html
import logging
import re
from datetime import datetime, timedelta, timezone

import requests

from ..models import Job
from ..retry import with_retry
from ..timeutil import normalize_timestamp

log = logging.getLogger(__name__)
TIMEOUT = 20
SEARCH_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://hn.algolia.com/api/v1/items/{story_id}"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                         "AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/126.0 Safari/537.36"}

_STRIP_HTML = re.compile(r"<[^>]+>")
_HIRING_PREFIX = "ask hn: who is hiring?"


def _clean(text: str) -> str:
    text = _STRIP_HTML.sub(" ", _html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


@with_retry(max_attempts=3, backoff_seconds=2.0,
            exceptions=(requests.RequestException,),
            source_name="hackernews")
def _search(params: dict) -> dict:
    resp = requests.get(SEARCH_URL, params=params, timeout=TIMEOUT,
                        headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


@with_retry(max_attempts=3, backoff_seconds=2.0,
            exceptions=(requests.RequestException,),
            source_name="hackernews")
def _fetch_item(story_id: int) -> dict:
    resp = requests.get(ITEM_URL.format(story_id=story_id), timeout=TIMEOUT,
                        headers=HEADERS)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, dict) else {}


def _find_thread(months_back: int = 2) -> dict | None:
    """Latest 'Ask HN: Who is hiring?' story within months_back."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months_back)
    data = _search({"tags": "story,author_whoishiring", "hitsPerPage": 30})
    candidates = []
    for hit in data.get("hits", []) or []:
        if not isinstance(hit, dict):
            continue
        title = str(hit.get("title") or "")
        low = title.strip().lower()
        if not low.startswith(_HIRING_PREFIX):
            continue  # skips "Who wants to be hired" + freelancer threads
        if "freelanc" in low:
            continue
        created = normalize_timestamp(hit.get("created_at"))
        try:
            dt = datetime.fromisoformat(created) if created else None
        except ValueError:
            dt = None
        if dt is None or dt < cutoff:
            continue
        candidates.append((dt, hit))
    if not candidates:
        return None
    candidates.sort(key=lambda c: c[0], reverse=True)
    return candidates[0][1]


def _fetch_comments_fallback(story_id: int) -> list[dict]:
    """Page search_by_date comment hits when /items lacks the children tree."""
    out: list[dict] = []
    for page in range(0, 20):
        data = _search({"tags": f"comment,story_{story_id}",
                        "page": page, "hitsPerPage": 100})
        hits = data.get("hits", []) or []
        out.extend(h for h in hits if isinstance(h, dict))
        if page >= int(data.get("nbPages", 1) or 1) - 1 or not hits:
            break
    return out


def _parse_comment(story_id: int, comment: dict) -> Job | None:
    if not isinstance(comment, dict):
        return None
    if comment.get("deleted") or comment.get("dead"):
        return None
    cid = comment.get("id") or comment.get("objectID")
    if not cid:
        return None
    raw = str(comment.get("text") or "")
    if not raw.strip():
        return None
    # First line: text before the first paragraph break / newline.
    first_html = re.split(r"<p>|<br\s*/?>|\n", raw, maxsplit=1)[0]
    first_line = _clean(first_html)
    body = _clean(raw)
    parts = [p.strip() for p in first_line.split("|")]
    parts = [p for p in parts if p]
    if len(parts) >= 2:
        company, title = parts[0], parts[1]
        location = parts[2] if len(parts) >= 3 else ""
    elif len(parts) == 1:
        company, title, location = "", parts[0][:120], ""
    else:
        company, title, location = "", body[:120], ""
    if not title:
        return None
    is_remote = bool(re.search(r"\b(remote|work from home|distributed)\b",
                               first_line, re.IGNORECASE))
    description = first_line + ("\n" + body[:400] if body else "")
    job = Job(
        title=title,
        company=company,
        description=description[:4000],
        location=location or ("Remote" if is_remote else ""),
        url=f"https://news.ycombinator.com/item?id={cid}",
        posted_date=normalize_timestamp(comment.get("created_at")),
        source="hackernews",
        is_remote=is_remote,
    )
    return job.clean()


def fetch_hackernews_jobs(months_back: int = 2) -> list[Job]:
    """Fetch direct-child postings of the latest hiring thread."""
    thread = _find_thread(months_back)
    if not thread:
        log.warning("HackerNews: no hiring thread found in the last %s "
                    "month(s)", months_back)
        return []
    story_id = int(thread.get("objectID") or 0)
    if not story_id:
        return []
    log.info("HackerNews: using thread %r (id=%d)", thread.get("title"),
             story_id)
    try:
        item = _fetch_item(story_id)
    except Exception as exc:
        log.error("HackerNews thread fetch failed: %s", exc)
        raise
    children = item.get("children")
    if isinstance(children, list) and children:
        comments = [c for c in children
                    if isinstance(c, dict)
                    and c.get("parent_id", story_id) == story_id]
    else:
        log.warning("HackerNews: /items response lacks the children tree; "
                    "falling back to comment search pages")
        comments = _fetch_comments_fallback(story_id)
    cutoff = datetime.now(timezone.utc) - timedelta(days=30 * months_back)
    jobs: list[Job] = []
    seen: set[str] = set()
    for comment in comments:
        try:
            posted = normalize_timestamp(comment.get("created_at"))
            try:
                if posted and datetime.fromisoformat(posted) < cutoff:
                    continue
            except ValueError:
                pass
            job = _parse_comment(story_id, comment)
            if job is not None and job.url not in seen:
                seen.add(job.url)
                jobs.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed hackernews comment: %s", exc)
    return jobs
