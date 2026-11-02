"""Config-driven match profile (YAML, not hardcoded in scoring logic)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class MatchProfile:
    profile_text: str = ""
    target_roles: list[str] = field(default_factory=list)
    boost_keywords: list[str] = field(default_factory=list)
    title_boost: float = 0.15
    keyword_boost: float = 0.03
    keyword_boost_cap: float = 0.15
    experience_threshold_years: int = 5
    experience_penalty_per_year: float = 0.05
    experience_penalty_cap: float = 0.25
    location_penalty: float = 0.20
    home_location: str = "Lagos"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    # Remote eligibility (Part 5): bonus for worldwide-remote, penalty for
    # restricted-remote (unknown/onsite get no change).
    remote_worldwide_boost: float = 0.0
    remote_restricted_penalty: float = 0.0
    # Relocation block (default disabled): skip the onsite penalty for jobs
    # in preferred_countries and boost visa/relocation-support mentions.
    relocation_enabled: bool = False
    relocation_countries: list[str] = field(default_factory=list)
    visa_sponsorship_boost: float = 0.0


def load_profile(path: str | Path) -> MatchProfile:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("profile.yaml needs PyYAML (uv sync)") from exc
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"Profile {path} must be a mapping at the top level")

    def _str_list(key: str) -> list[str]:
        v = data.get(key, [])
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v]

    reloc = data.get("relocation", {}) or {}
    if not isinstance(reloc, dict):
        reloc = {}
    rel_countries = reloc.get("preferred_countries", []) or []
    if isinstance(rel_countries, str):
        rel_countries = [rel_countries]
    return MatchProfile(
        profile_text=str(data.get("profile_text", "") or ""),
        target_roles=_str_list("target_roles"),
        boost_keywords=_str_list("boost_keywords"),
        title_boost=float(data.get("title_boost", 0.15)),
        keyword_boost=float(data.get("keyword_boost", 0.03)),
        keyword_boost_cap=float(data.get("keyword_boost_cap", 0.15)),
        experience_threshold_years=int(data.get("experience_threshold_years", 5)),
        experience_penalty_per_year=float(data.get("experience_penalty_per_year", 0.05)),
        experience_penalty_cap=float(data.get("experience_penalty_cap", 0.25)),
        location_penalty=float(data.get("location_penalty", 0.20)),
        remote_worldwide_boost=float(data.get("remote_worldwide_boost", 0.0)),
        remote_restricted_penalty=float(
            data.get("remote_restricted_penalty", 0.0)),
        relocation_enabled=bool(reloc.get("enabled", False)),
        relocation_countries=[str(x) for x in rel_countries],
        visa_sponsorship_boost=float(reloc.get("visa_sponsorship_boost", 0.0)),
        home_location=str(data.get("home_location", "Lagos") or "Lagos"),
        embedding_model=str(
            data.get("embedding_model", "BAAI/bge-small-en-v1.5")
            or "BAAI/bge-small-en-v1.5"
        ),
    )
