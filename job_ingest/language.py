"""Language gate: drop non-English / foreign-language-requirement postings.

Applied to every source before insert (per-source removal counts logged).
All operative values come from the config.yaml ``language_gate`` block:

  language_gate:
    enabled: true
    blocked_patterns: [case-insensitive regexes ...]  # requirement phrasing
    min_english_ratio: 0.10
    stopwords: [...]  # optional override of the built-in common-word list

Two checks (a posting fails if EITHER fires):
1. blocked_patterns: requirement phrasing for German, Swedish, Dutch and
   French (fluent/native/required/C1 phrasing, Deutschkenntnisse,
   fliessend Deutsch, svenska) — "X is a plus" mentions are NOT blocked
   (patterns require requirement verbs, not bare language names).
2. English check: the share of common English stopwords among tokens in
   the first 1500 characters of the description. Below min_english_ratio
   the posting is treated as non-English. The built-in stopword list below
   is linguistic infrastructure (like the experience regex shapes in
   job_match/rules.py), not job-domain keywords; it can be overridden or
   extended from config, and an empty/missing config disables each check.
"""
from __future__ import annotations

import logging
import re

log = logging.getLogger(__name__)

# Common English function words for the lightweight English check.
# Overridable via language_gate.stopwords in config.yaml.
DEFAULT_STOPWORDS = [
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "it",
    "for", "not", "on", "with", "he", "as", "you", "do", "at", "this",
    "but", "his", "by", "from", "they", "we", "say", "her", "she", "or",
    "an", "will", "my", "one", "all", "would", "there", "their", "what",
    "so", "up", "out", "if", "about", "who", "get", "which", "go", "me",
    "when", "make", "can", "like", "time", "just", "him", "know", "take",
    "into", "year", "your", "good", "some", "them", "see", "other",
    "than", "then", "now", "look", "only", "come", "its", "over",
    "think", "also", "back", "after", "use", "two", "how", "our",
    "work", "well", "even", "because", "any", "these", "us",
]


def english_ratio(text: str, stopwords: list[str]) -> float:
    """Share of tokens in text that are common English stopwords."""
    toks = re.findall(r"[a-z']+", str(text or "").lower())
    if not toks:
        return 0.0
    stop = set(stopwords)
    hits = sum(1 for t in toks if t in stop)
    return hits / len(toks)


def passes_language_gate(title: str, description: str,
                         section: dict) -> tuple[bool, str]:
    """(passes, reason). Empty reason when passing.

    section is the raw language_gate mapping from config. Missing/empty
    blocked_patterns disables check 1; missing min_english_ratio (or 0)
    disables check 2; enabled:false passes everything.
    """
    section = section or {}
    if not section.get("enabled", True):
        return True, ""
    text = f"{title or ''}\n{description or ''}"
    patterns = [p for p in (section.get("blocked_patterns") or [])
                if str(p).strip()]
    for pat in patterns:
        try:
            if re.search(str(pat), text, re.IGNORECASE):
                return False, f"blocked-pattern:{pat}"
        except re.error as exc:
            log.warning("Bad language_gate regex %r: %s", pat, exc)
    try:
        min_ratio = float(section.get("min_english_ratio", 0.0) or 0.0)
    except (TypeError, ValueError):
        min_ratio = 0.0
    if min_ratio > 0:
        stopwords = section.get("stopwords") or DEFAULT_STOPWORDS
        head = str(description or "")[:1500] or str(title or "")
        ratio = english_ratio(head, [str(w) for w in stopwords])
        if ratio < min_ratio:
            return False, f"non-english:{ratio:.3f}<{min_ratio}"
    return True, ""
