"""Rule-based adjustments layered on the embedding base score.

All roles/keywords/thresholds come from the MatchProfile (profile.yaml);
nothing is hardcoded here besides the regex shapes for experience parsing.
"""
from __future__ import annotations

import re

from .profile import MatchProfile

# Catches "5+ years", "3-5 years", "minimum 4 years experience", "7 years
# of experience", "5 YOE". Returns every mentioned number; caller takes max.
_YEARS_PATTERNS = [
    re.compile(r"(\d+)\s*\+\s*years?", re.IGNORECASE),
    re.compile(r"(\d+)\s*[-–]\s*(\d+)\s*years?", re.IGNORECASE),
    re.compile(r"minimum.{0,30}?(\d+)\s*(?:\+?\s*)?years?", re.IGNORECASE),
    re.compile(r"(\d+)\s*years?.{0,30}?(?:experience|exp\b)", re.IGNORECASE),
    re.compile(r"(?:experience|exp\b).{0,30}?(\d+)\s*years?", re.IGNORECASE),
    re.compile(r"(\d+)\s*yoe\b", re.IGNORECASE),
]


def extract_required_years(description: str | None) -> int | None:
    """Max years-of-experience mentioned, or None if none found."""
    text = str(description or "")
    if not text.strip():
        return None
    found: list[int] = []
    for pat in _YEARS_PATTERNS:
        for m in pat.finditer(text):
            found.extend(int(g) for g in m.groups() if g is not None and g.isdigit())
    return max(found) if found else None


def _word_hit(haystack: str, needle: str) -> bool:
    # Whole-word match so "RAG" doesn't hit "storage", etc.
    return re.search(r"\b" + re.escape(needle) + r"\b", haystack,
                     re.IGNORECASE) is not None


def title_boost(title: str | None, profile: MatchProfile) -> tuple[float, str | None]:
    t = str(title or "").lower()
    for role in profile.target_roles:
        if role.lower() in t:
            return profile.title_boost, role
    return 0.0, None


def keyword_boost(description: str | None,
                  profile: MatchProfile) -> tuple[float, list[str]]:
    text = str(description or "")
    hits = [kw for kw in profile.boost_keywords if _word_hit(text, kw)]
    boost = min(profile.keyword_boost * len(hits), profile.keyword_boost_cap)
    return boost, hits


def experience_penalty(description: str | None,
                       profile: MatchProfile) -> tuple[float, int | None]:
    years = extract_required_years(description)
    if years is None or years <= profile.experience_threshold_years:
        return 0.0, years
    over = years - profile.experience_threshold_years
    penalty = min(profile.experience_penalty_per_year * over,
                  profile.experience_penalty_cap)
    return penalty, years


def location_penalty(location: str | None, is_remote: bool,
                     profile: MatchProfile) -> tuple[float, str]:
    loc = str(location or "").lower()
    if bool(is_remote) or "remote" in loc:
        return 0.0, "remote"
    if profile.home_location.lower() in loc:
        return 0.0, "home"
    # Relocation block (default disabled): no onsite penalty for jobs in
    # a preferred country.
    if profile.relocation_enabled:
        for country in profile.relocation_countries:
            if country and _word_hit(loc, str(country).lower()):
                return 0.0, "relocation-preferred"
    return profile.location_penalty, "onsite-other"


# Visa/relocation-support mention shapes (mechanism, not weights — the
# boost value lives in profile.yaml under relocation).
_VISA_PATTERNS = [
    re.compile(r"visa\s+sponsorship", re.IGNORECASE),
    re.compile(r"visa\s+sponsored", re.IGNORECASE),
    re.compile(r"sponsor\w*\s+visas?", re.IGNORECASE),
    re.compile(r"relocation\s+(support|assistance|package)", re.IGNORECASE),
]


def visa_boost(description: str | None,
               profile: MatchProfile) -> tuple[float, str | None]:
    """Relocation-block boost for visa/relocation mentions (0 when disabled)."""
    if not profile.relocation_enabled or profile.visa_sponsorship_boost <= 0:
        return 0.0, None
    text = str(description or "")
    for pat in _VISA_PATTERNS:
        m = pat.search(text)
        if m:
            return profile.visa_sponsorship_boost, m.group(0)
    return 0.0, None


def remote_scope_adjustment(remote_scope: str | None,
                            profile: MatchProfile) -> tuple[float, str]:
    """Score delta for the remote eligibility scope (Part 5).

    worldwide -> +remote_worldwide_boost; restricted ->
    -remote_restricted_penalty; unknown/onsite/empty -> no change.
    """
    scope = str(remote_scope or "").strip().lower()
    if scope == "worldwide" and profile.remote_worldwide_boost:
        return profile.remote_worldwide_boost, "worldwide"
    if scope == "restricted" and profile.remote_restricted_penalty:
        return -profile.remote_restricted_penalty, "restricted"
    return 0.0, scope or "onsite-or-unknown"
