"""Remote eligibility from Nigeria: worldwide / restricted / unknown.

Set ONLY for remote postings (is_remote or "remote" in the location);
onsite postings keep an empty scope. Lists come from the config.yaml
``remote_scope`` block:

  remote_scope:
    worldwide_terms: [...]    # whole-word matched (multi-word phrases ok)
    restricted_patterns: [...]  # case-insensitive regexes

Restricted wins over worldwide (an explicit exclusion beats a broad
"remote" claim). CET / UK-timezone requirements are deliberately NOT
restricted (Lagos is UTC+1). Unknown (neither signal) gets no score
change downstream.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)


def _is_remote_posting(title: str, location: str, is_remote: bool) -> bool:
    if bool(is_remote):
        return True
    return bool(re.search(r"\bremote\b",
                          f"{title or ''} {location or ''}", re.IGNORECASE))


def classify_remote_scope(title: str, location: str, description: str,
                          is_remote: bool, section: dict) -> str:
    """worldwide | restricted | unknown ("" when not a remote posting)."""
    if not _is_remote_posting(title, location, is_remote):
        return ""
    section = section or {}
    text = f"{title or ''}\n{location or ''}\n{description or ''}"
    for pat in (section.get("restricted_patterns") or []):
        if not str(pat).strip():
            continue
        try:
            if re.search(str(pat), text, re.IGNORECASE):
                return "restricted"
        except re.error as exc:
            log.warning("Bad remote_scope regex %r: %s", pat, exc)
    for term in (section.get("worldwide_terms") or []):
        term = str(term).strip()
        if not term:
            continue
        if re.search(r"\b" + re.escape(term) + r"\b", text, re.IGNORECASE):
            return "worldwide"
    return "unknown"
