"""Pre-insert relevance gate: role-category + seniority filtering.

A job passes only if:
  1. a role_categories keyword matches in the title or in the first 200
     characters of the description (the role hook — full-body matching let
     incidental mentions like "data scientist" in unrelated postings pass).
     Role matching is whole-word (ported from job_match/rules.py), so short
     tokens like "llm" cannot hit inside larger words ("fulfillment"), AND
  2. its title has no seniority exclude_keyword, unless an include_keyword
     is also in the title (e.g. "Mid-level Backend Engineer (Senior team)"), AND
  3. it is not an intern title (unless exclude_interns is off), AND
  4. its required years-of-experience (same extraction as job_match/rules.py)
     does not exceed max_years_experience.

All lists/thresholds come from config.yaml via the Config object; nothing is
hardcoded here. When strict_filtering is false, everything passes (testing).
"""
from __future__ import annotations

import logging
import re

from .config import Config
from .models import Job

log = logging.getLogger(__name__)


def _hits(text: str, keywords: list[str]) -> list[str]:
    """Substring match — kept for seniority title gating (unchanged)."""
    t = text.lower()
    return [kw for kw in keywords if kw and kw.lower() in t]


def _word_hits(text: str, keywords: list[str]) -> list[str]:
    """Whole-word match for role_categories (ported from job_match/rules)."""
    return [kw for kw in keywords if kw and _word_hit(text, kw)]


def _word_hit(haystack: str, needle: str) -> bool:
    # Whole-word match ported from job_match/rules.py: "llm" must not hit
    # "fulfillment", "RAG" must not hit "storage", etc.
    return re.search(r"\b" + re.escape(needle) + r"\b", haystack,
                     re.IGNORECASE) is not None


def job_passes_filters(job: Job, cfg: Config) -> bool:
    """True if the job may enter the DB. Takes the job plus its config
    (single job in, bool out; no globals, no hardcoded lists)."""
    if not cfg.strict_filtering:
        return True
    title = str(job.title or "")
    desc = str(job.description or "")
    # Role hook: title or description head only — never the full body.
    head = f"{title}\n{desc.strip()[:200]}"

    wanted = [kw for cat in cfg.role_categories for kw in cat.keywords]
    if wanted and not _word_hits(head, wanted):
        return False

    sen = cfg.seniority
    if sen.exclude_interns and "intern" in title.lower():
        return False
    if _hits(title, sen.exclude_keywords) and not _hits(title, sen.include_keywords):
        return False

    if sen.max_years_experience is not None:
        try:
            from job_match.rules import extract_required_years
        except ImportError:
            log.warning("job_match not importable; skipping YoE check")
            return True
        years = extract_required_years(desc or None)
        if years is not None and years > sen.max_years_experience:
            return False
    return True
