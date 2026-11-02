"""Startup liveness check for ATS board slugs (Greenhouse / Lever / Ashby).

Each configured slug is requested exactly once per run (short timeout, no
retry — this is a liveness probe, not a fetch). HTTP 404 means the board
is dead and the slug is skipped for the run. Any other failure (timeout,
403, 5xx, parse error) is treated as transient: the slug is KEPT so a
blip does not silently drop a live board. Exactly one summary line is
logged per run listing the dead slugs (or that all slugs are live).

All slugs come from config (no hardcoding here); callers pass the three
configured lists in and get the live subsets back plus the dead list.
"""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)

TIMEOUT = 15
HEADERS = {"User-Agent": "jobtool/1.0"}


def _probe(url: str) -> str:
    """One unretried GET. Returns 'live' | 'dead' | 'transient'."""
    try:
        resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    except (requests.Timeout, requests.ConnectionError,
            TimeoutError, ConnectionError):
        return "transient"
    except requests.RequestException:
        return "transient"
    if resp.status_code == 404:
        return "dead"
    if 200 <= resp.status_code < 300:
        return "live"
    return "transient"


def check_ats_slugs(greenhouse: list[str], lever: list[str],
                    ashby: list[str]
                    ) -> tuple[list[str], list[str], list[str], list[str]]:
    """Probe every configured slug once. Returns
    (live_greenhouse, live_lever, live_ashby, dead_labels)."""
    live_gh: list[str] = []
    live_lever: list[str] = []
    live_ashby: list[str] = []
    dead: list[str] = []
    for slug in greenhouse:
        state = _probe(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if state == "dead":
            dead.append(f"greenhouse:{slug}")
        else:
            live_gh.append(slug)
    for slug in lever:
        state = _probe(f"https://api.lever.co/v0/postings/{slug}")
        if state == "dead":
            dead.append(f"lever:{slug}")
        else:
            live_lever.append(slug)
    for slug in ashby:
        state = _probe(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if state == "dead":
            dead.append(f"ashby:{slug}")
        else:
            live_ashby.append(slug)
    total = len(greenhouse) + len(lever) + len(ashby)
    if dead:
        log.info("ATS slug check: %d dead slug(s) skipped for this run: %s "
                 "(%d/%d live)", len(dead), ", ".join(dead),
                 total - len(dead), total)
    else:
        log.info("ATS slug check: all %d slug(s) live, none skipped", total)
    return live_gh, live_lever, live_ashby, dead
