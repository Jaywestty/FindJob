"""Polite crawler layer for HTML board sources (Swedish/Nigerian sites).

Behaviour (all bounds from config.yaml ``crawler`` block, never hardcoded
in calling sources):

- fetch(url): identifying User-Agent, (connect, read) timeouts, retry via
  job_ingest.retry.with_retry (only timeouts / connection errors / 429 /
  5xx are retried), and per-host rate limiting.
- robots.txt: checked once per host per run via urllib.robotparser (cached).
  Any URL robots.txt disallows is refused (None) and logged. Crawl-delay is
  honored as max(declared delay, configured default of 1.5 s).
- Detail-page cap: fetch_detail() counts per run; once
  max_detail_fetches_per_run (default 150) is reached, further detail
  fetches are refused (None) and logged. Sitemap/robots/API-listing
  fetches do not count against the cap.
- read_sitemap(): follows sitemap indexes, urlsets and gzip payloads and
  returns (url, lastmod) pairs, filterable by URL regex and by lastmod
  within the last N days.
- extract_jobposting(): schema.org JobPosting JSON-LD (title,
  hiringOrganization.name, description HTML-stripped, jobLocation address,
  datePosted, baseSalary, employmentType, jobLocationType TELECOMMUTE ->
  is_remote). extract_fallback(): BeautifulSoup selectors when JSON-LD is
  absent (h1 title, .job-description / article / main body).
"""
from __future__ import annotations

import gzip
import logging
import re
import sqlite3
import time
import urllib.parse
import urllib.robotparser as _robotparser
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from .retry import with_retry

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "JobToolBot/1.0 (+personal research project; polite crawler; "
    "respects robots.txt)"
)
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30

_STRIP_HTML = re.compile(r"<[^>]+>")

# Module-level per-run total: one ingest process == one run, so this is
# the run-wide count of detail-page fetches across all crawler sources.
_RUN_DETAIL_FETCHES = 0


def clean_html(text: str) -> str:
    """Strip tags + collapse whitespace (shared by crawler-based sources)."""
    import html as _html
    text = _STRIP_HTML.sub(" ", _html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Crawler:
    """Per-run polite fetcher. One instance per source per run."""

    source_name: str = "crawler"
    max_detail_fetches: int = 150
    default_delay_seconds: float = 1.5
    user_agent: str = DEFAULT_USER_AGENT
    detail_fetches: int = 0
    _last_fetch: dict[str, float] = field(default_factory=dict)
    _robots: dict[str, tuple[_robotparser.RobotFileParser, float]] = field(
        default_factory=dict)

    def __post_init__(self) -> None:
        if not self.user_agent:
            self.user_agent = DEFAULT_USER_AGENT

    @property
    def headers(self) -> dict[str, str]:
        return {"User-Agent": self.user_agent or DEFAULT_USER_AGENT}

    # -- robots.txt -----------------------------------------------------
    def _robots_for(self, host: str, scheme: str) -> tuple[
            _robotparser.RobotFileParser, float]:
        cached = self._robots.get(host)
        if cached is not None:
            return cached
        rp = _robotparser.RobotFileParser()
        robots_url = f"{scheme}://{host}/robots.txt"
        declared_delay: float | None = None
        try:
            resp = requests.get(robots_url, timeout=(CONNECT_TIMEOUT,
                                                     READ_TIMEOUT),
                                headers=self.headers)
            if resp.status_code == 200 and resp.text.strip():
                rp.parse(resp.text.splitlines())
                log.info("[%s] robots.txt checked for %s (%d rules)",
                         self.source_name, host, len(resp.text.splitlines()))
            else:
                rp.parse([])
                log.info("[%s] robots.txt checked for %s: HTTP %d "
                         "(treating as allow-all)", self.source_name, host,
                         resp.status_code)
            try:
                declared_delay = rp.crawl_delay(self.user_agent) or \
                    rp.crawl_delay("*")
            except Exception:  # noqa: BLE001
                declared_delay = None
        except Exception as exc:  # noqa: BLE001
            rp.parse([])
            log.info("[%s] robots.txt checked for %s: unreachable (%s); "
                     "treating as allow-all", self.source_name, host, exc)
        delay = max(float(declared_delay or 0.0),
                    float(self.default_delay_seconds))
        self._robots[host] = (rp, delay)
        return rp, delay

    def allowed(self, url: str) -> bool:
        """True if robots.txt allows url (checks + caches per host)."""
        try:
            parts = urllib.parse.urlsplit(url)
            host = parts.hostname or ""
            if not host:
                return False
            rp, _ = self._robots_for(host, parts.scheme or "https")
            ok = rp.can_fetch(self.user_agent, url)
            if not ok:
                log.info("[%s] robots.txt REFUSED %s", self.source_name, url)
            return ok
        except Exception as exc:  # noqa: BLE001
            log.warning("[%s] robots check failed for %s (%s); refusing",
                        self.source_name, url, exc)
            return False

    def host_delay(self, url: str) -> float:
        try:
            parts = urllib.parse.urlsplit(url)
            _, delay = self._robots_for(parts.hostname or "",
                                        parts.scheme or "https")
            return delay
        except Exception:  # noqa: BLE001
            return float(self.default_delay_seconds)

    # -- fetching --------------------------------------------------------
    def _polite_wait(self, url: str) -> None:
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
        except Exception:  # noqa: BLE001
            return
        delay = self.host_delay(url)
        last = self._last_fetch.get(host, 0.0)
        wait = delay - (time.monotonic() - last)
        if wait > 0:
            time.sleep(wait)

    @with_retry(max_attempts=3, backoff_seconds=2.0,
                exceptions=(requests.RequestException,),
                source_name="crawler")
    def _get(self, url: str) -> requests.Response:
        resp = requests.get(url, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                            headers=self.headers)
        resp.raise_for_status()
        return resp

    def fetch(self, url: str) -> str | None:
        """Fetch any URL (sitemaps/listings). Robots-checked + rate-limited.

        Returns text, or None when robots.txt disallows the URL. Raises on
        HTTP/network failure (retry policy applies)."""
        if not self.allowed(url):
            return None
        self._polite_wait(url)
        resp = self._get(url)
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
            self._last_fetch[host] = time.monotonic()
        except Exception:  # noqa: BLE001
            pass
        return resp.text

    def fetch_bytes(self, url: str) -> bytes | None:
        """Like fetch() but returns raw bytes (for gzip sitemaps)."""
        if not self.allowed(url):
            return None
        self._polite_wait(url)
        resp = self._get(url)
        try:
            host = urllib.parse.urlsplit(url).hostname or ""
            self._last_fetch[host] = time.monotonic()
        except Exception:  # noqa: BLE001
            pass
        return resp.content

    def fetch_detail(self, url: str) -> str | None:
        """Fetch one job detail page, counting against the per-run cap.

        Returns None when robots.txt disallows the URL or when the cap is
        already reached (both cases logged)."""
        global _RUN_DETAIL_FETCHES
        if (_RUN_DETAIL_FETCHES >= self.max_detail_fetches
                or self.detail_fetches >= self.max_detail_fetches):
            log.info("[%s] detail-fetch cap reached (%d/run); skipping %s",
                     self.source_name, self.max_detail_fetches, url)
            return None
        html = self.fetch(url)
        if html is None:
            return None
        self.detail_fetches += 1
        _RUN_DETAIL_FETCHES += 1
        return html

    # -- sitemaps ----------------------------------------------------------
    def read_sitemap(self, url: str, url_pattern: str | None = None,
                     days: int | None = None) -> list[tuple[str, str | None]]:
        """Return (loc, lastmod) pairs from a sitemap URL.

        Handles sitemap indexes (recursed), urlsets and gzip payloads.
        url_pattern filters by regex on the URL; days keeps only entries
        with a lastmod within the last N days (undated entries are dropped
        when days is set). Robots-checked like any other fetch.
        """
        from datetime import datetime, timezone
        found: list[tuple[str, str | None]] = []

        def _read_one(u: str, depth: int = 0) -> None:
            if depth > 3:
                return
            raw = self.fetch_bytes(u)
            if not raw:
                return
            if raw[:2] == b"\x1f\x8b" or u.endswith(".gz"):
                try:
                    raw = gzip.decompress(raw)
                except OSError as exc:
                    log.warning("[%s] bad gzip sitemap %s: %s",
                                self.source_name, u, exc)
                    return
            try:
                root = ET.fromstring(raw)
            except ET.ParseError as exc:
                log.warning("[%s] bad XML sitemap %s: %s",
                            self.source_name, u, exc)
                return
            tag = root.tag.split("}")[-1].lower()
            ns = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
            if tag == "sitemapindex":
                for sm in root.findall("s:sitemap", ns):
                    loc = (sm.findtext("s:loc", default="", namespaces=ns)
                           or "").strip()
                    if loc:
                        _read_one(loc, depth + 1)
                return
            for item in root.findall("s:url", ns):
                loc = (item.findtext("s:loc", default="", namespaces=ns)
                       or "").strip()
                lastmod = (item.findtext("s:lastmod", default="",
                                         namespaces=ns) or "").strip() or None
                if loc:
                    found.append((loc, lastmod))

        _read_one(url)
        if url_pattern:
            rx = re.compile(url_pattern)
            found = [(u, lm) for u, lm in found if rx.search(u)]
        if days is not None and days >= 0:
            from datetime import timedelta
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            kept: list[tuple[str, str | None]] = []
            for u, lm in found:
                if not lm:
                    continue
                try:
                    dt = datetime.fromisoformat(lm.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    if dt >= cutoff:
                        kept.append((u, lm))
                except ValueError:
                    continue
            found = kept
        log.info("[%s] sitemap %s -> %d url(s)", self.source_name, url,
                 len(found))
        return found


def existing_urls(db_path: str, urls: list[str]) -> set[str]:
    """URLs already present in jobs.db (skip before fetching details)."""
    urls = [u for u in urls if u]
    if not urls:
        return set()
    try:
        with sqlite3.connect(db_path) as conn:
            have: set[str] = set()
            for i in range(0, len(urls), 500):
                chunk = urls[i:i + 500]
                q = ("SELECT url FROM jobs WHERE url IN (%s)"
                     % ",".join("?" * len(chunk)))
                have.update(r[0] for r in conn.execute(q, chunk).fetchall())
            return have
    except Exception as exc:  # noqa: BLE001
        log.warning("existing_urls check failed (%s); fetching all", exc)
        return set()


# -- JobPosting extraction ------------------------------------------------

def _iter_ld_blocks(data: object):
    if isinstance(data, dict):
        if "@graph" in data and isinstance(data["@graph"], list):
            yield from data["@graph"]
        else:
            yield data
    elif isinstance(data, list):
        for item in data:
            yield from _iter_ld_blocks(item)


def _is_job_posting(block: dict) -> bool:
    t = block.get("@type")
    if isinstance(t, list):
        return any(str(x).lower() == "jobposting" for x in t)
    return str(t or "").lower() == "jobposting"


def _address_str(addr: object) -> str:
    if not addr:
        return ""
    if isinstance(addr, str):
        return addr.strip()
    if isinstance(addr, list):
        return ", ".join(_address_str(a) for a in addr if _address_str(a))
    if isinstance(addr, dict):
        parts = [addr.get("addressLocality"), addr.get("addressRegion"),
                 addr.get("addressCountry")]
        return ", ".join(str(p).strip() for p in parts if str(p or "").strip())
    return ""


def _hiring_org(block: dict) -> str:
    org = block.get("hiringOrganization") or {}
    if isinstance(org, dict):
        name = org.get("name") or ""
        return str(name).strip()
    return str(org or "").strip()


def _salary_range(block: dict) -> tuple[float | None, float | None]:
    sal = block.get("baseSalary") or {}
    if not isinstance(sal, dict):
        try:
            f = float(sal)  # type: ignore[arg-type]
            return (f if f > 0 else None), None
        except (TypeError, ValueError):
            return None, None
    val = sal.get("value") or {}
    if isinstance(val, dict):
        lo, hi = val.get("minValue"), val.get("maxValue")
        if lo is None and hi is None and val.get("value") is not None:
            lo = hi = val.get("value")
    else:
        lo = hi = val
    def _f(v: object) -> float | None:
        try:
            f = float(v)  # type: ignore[arg-type]
            return f if f > 0 else None
        except (TypeError, ValueError):
            return None
    return _f(lo), _f(hi)


def extract_jobposting(html: str) -> dict | None:
    """Parse schema.org JobPosting JSON-LD. Returns normalized fields or None.

    Fields: title, company, description (HTML stripped), location,
    date_posted, salary_min, salary_max, employment_type, is_remote
    (jobLocationType == TELECOMMUTE maps to remote).
    """
    import json as _json
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = _json.loads(raw)
        except Exception:  # noqa: BLE001
            # Some boards embed raw control characters / literal newlines
            # inside JSON strings (illegal in strict JSON); sanitize and
            # retry with strict=False before giving up on the block.
            try:
                data = _json.loads(re.sub(r"[\x00-\x09\x0b\x0c\x0e-\x1f]",
                                          " ", raw), strict=False)
            except Exception:  # noqa: BLE001
                continue
        for block in _iter_ld_blocks(data):
            if not isinstance(block, dict) or not _is_job_posting(block):
                continue
            loc = block.get("jobLocation")
            locs: list[str] = []
            for item in (loc if isinstance(loc, list) else [loc]):
                if isinstance(item, dict):
                    a = _address_str(item.get("address"))
                    if a:
                        locs.append(a)
                elif item:
                    s = _address_str(item)
                    if s:
                        locs.append(s)
            loc_str = ", ".join(locs)
            loc_types = block.get("jobLocationType")
            types = ([loc_types] if isinstance(loc_types, str)
                     else list(loc_types or []))
            remote = any(str(t).upper() == "TELECOMMUTE" for t in types)
            if remote and not loc_str:
                loc_str = "Remote"
            smin, smax = _salary_range(block)
            desc = block.get("description") or ""
            return {
                "title": str(block.get("title") or "").strip(),
                "company": _hiring_org(block),
                "description": clean_html(str(desc)),
                "location": loc_str,
                "date_posted": str(block.get("datePosted") or "").strip()
                or None,
                "salary_min": smin,
                "salary_max": smax,
                "employment_type": str(block.get("employmentType") or "")
                .strip() or None,
                "is_remote": remote,
            }
    return None


def extract_fallback(html: str) -> dict:
    """BeautifulSoup fallback when JSON-LD is absent: h1 title + best body."""
    soup = BeautifulSoup(html, "html.parser")
    for junk in soup(["script", "style", "nav", "header", "footer"]):
        junk.decompose()
    title = ""
    h1 = soup.find("h1")
    if h1:
        title = h1.get_text(" ", strip=True)
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    body = (soup.select_one(".job-description") or soup.find("article")
            or soup.find("main") or soup.body or soup)
    text = body.get_text(" ", strip=True) if body else ""
    text = re.sub(r"\s+", " ", text).strip()
    return {"title": title, "description": text}
