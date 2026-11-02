"""Config loading (JSON or YAML)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class LocationConfig:
    """One searchable location with its own region-scoped JobSpy settings."""
    name: str = ""
    region: str = "global"
    country_indeed: str | None = None
    sites: list[str] = field(default_factory=lambda: ["linkedin"])


@dataclass
class RoleCategory:
    """One wanted role family: id + title/description match keywords."""
    id: str = ""
    keywords: list[str] = field(default_factory=list)


@dataclass
class SeniorityFilter:
    """Title-based seniority gating + experience cap + intern handling."""
    exclude_keywords: list[str] = field(default_factory=list)
    include_keywords: list[str] = field(default_factory=list)
    exclude_interns: bool = True
    max_years_experience: int = 5


@dataclass
class Config:
    search_terms: list[str] = field(default_factory=list)
    locations: list[LocationConfig] = field(default_factory=list)
    hours_old: int = 72
    results_wanted: int = 50
    jobspy_sites: list[str] = field(
        default_factory=lambda: ["linkedin", "indeed", "glassdoor", "zip_recruiter"]
    )
    greenhouse_companies: list[str] = field(default_factory=list)
    lever_companies: list[str] = field(default_factory=list)
    ashby_companies: list[str] = field(default_factory=list)
    db_path: str = "jobs.db"
    retention_days: int = 3
    telegram_top_n: int = 10
    strict_filtering: bool = True
    role_categories: list[RoleCategory] = field(default_factory=list)
    seniority: SeniorityFilter = field(default_factory=SeniorityFilter)
    extra_sources: dict[str, bool] = field(default_factory=dict)
    extra_source_opts: dict[str, dict] = field(default_factory=dict)
    # Raw per-section mappings for crawler-driven sources and gates
    # (crawler, nordictechjobs, englishjobs, jobberman, myjobmag,
    # language_gate, remote_scope). Logic reads lists/thresholds from here;
    # nothing operative is hardcoded in the sources.
    sections: dict[str, dict] = field(default_factory=dict)

    def section(self, name: str) -> dict:
        """Raw config mapping for a named section ({} when unconfigured)."""
        v = self.sections.get(str(name).strip().lower(), {})
        return dict(v) if isinstance(v, dict) else {}

    def source_opts(self, name: str) -> dict:
        """Per-source options from extra_sources dict values ({} default)."""
        v = self.extra_source_opts.get(str(name).strip().lower(), {})
        return dict(v) if isinstance(v, dict) else {}


def _read_raw(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".json":
        data = json.loads(text)
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-not-found]
        except ImportError as exc:
            raise RuntimeError(
                f"Config file {path} is YAML but PyYAML is not installed "
                "(uv sync)"
            ) from exc
        data = yaml.safe_load(text)
    else:
        raise ValueError(f"Unsupported config extension: {path.suffix} (use .json/.yaml/.yml)")
    if not isinstance(data, dict):
        raise ValueError(f"Config {path} must be a mapping at the top level")
    return data


def _parse_locations(raw: object, default_sites: list[str]) -> list[LocationConfig]:
    """Parse the locations list. Accepts per-region objects; legacy plain
    strings are treated as global-region locations using default_sites."""
    if raw is None:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        raise ValueError("Config 'locations' must be a list")
    out: list[LocationConfig] = []
    for item in raw:
        if isinstance(item, str):
            out.append(LocationConfig(
                name=item, region="global",
                country_indeed=None, sites=list(default_sites),
            ))
        elif isinstance(item, dict):
            name = str(item.get("name", "") or "")
            if not name:
                raise ValueError(f"Location entry missing 'name': {item!r}")
            region = str(item.get("region", "global") or "global").strip().lower()
            country = item.get("country_indeed", None)
            country_indeed = None if country is None else str(country).strip() or None
            # Normalise "" / "null" / "none" strings to None
            if country_indeed is not None and country_indeed.lower() in ("null", "none"):
                country_indeed = None
            sites_raw = item.get("sites", None)
            if sites_raw is None:
                sites = list(default_sites)
            elif isinstance(sites_raw, str):
                sites = [sites_raw.strip().lower()]
            else:
                sites = [str(s).strip().lower() for s in sites_raw if str(s).strip()]
            if not sites:
                raise ValueError(f"Location {name!r} has an empty 'sites' list")
            out.append(LocationConfig(
                name=name, region=region,
                country_indeed=country_indeed, sites=sites,
            ))
        else:
            raise ValueError(f"Location entries must be strings or objects, got: {item!r}")
    return out


def _parse_role_categories(raw: object) -> list[RoleCategory]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("Config 'role_categories' must be a list")
    out: list[RoleCategory] = []
    for item in raw:
        if not isinstance(item, dict) or not str(item.get("id", "") or ""):
            raise ValueError(f"role_categories entries need an 'id': {item!r}")
        kws = item.get("keywords", [])
        if isinstance(kws, str):
            kws = [kws]
        out.append(RoleCategory(
            id=str(item["id"]).strip().lower(),
            keywords=[str(k).strip().lower() for k in kws if str(k).strip()],
        ))
    return out


def _parse_seniority(raw: object) -> SeniorityFilter:
    data = raw if isinstance(raw, dict) else {}

    def _kws(key: str) -> list[str]:
        v = data.get(key, [])
        if v is None:
            return []
        if isinstance(v, str):
            return [v.strip().lower()]
        return [str(x).strip().lower() for x in v if str(x).strip()]

    excl_interns = data.get("exclude_interns", True)
    return SeniorityFilter(
        exclude_keywords=_kws("exclude_keywords"),
        include_keywords=_kws("include_keywords"),
        exclude_interns=bool(excl_interns) if excl_interns is not None else True,
        max_years_experience=int(data.get("max_years_experience", 5)),
    )


def _parse_sections(data: dict, names: list[str]) -> dict[str, dict]:
    """Raw mapping sections (crawler options, per-source options, gates).

    Only the listed top-level keys are captured; each must be a mapping
    (or absent -> {}). Sources apply their own defaults for missing keys.
    """
    out: dict[str, dict] = {}
    for name in names:
        raw = data.get(name, {})
        if raw is None:
            out[name] = {}
        elif isinstance(raw, dict):
            out[name] = dict(raw)
        else:
            raise ValueError(f"Config '{name}' must be a mapping")
    return out


def _parse_extra_sources(raw: object) -> dict[str, bool]:
    """{source_name: enabled} mapping. Missing entries default to enabled."""
    if not isinstance(raw, dict):
        return {}
    out: dict[str, bool] = {}
    for k, v in raw.items():
        name = str(k).strip().lower()
        if not name:
            continue
        if isinstance(v, dict):
            out[name] = bool(v.get("enabled", True))
        else:
            out[name] = bool(v)
    return out


def _parse_extra_source_opts(raw: object) -> dict[str, dict]:
    """Per-source option mappings from extra_sources dict values.

    e.g. extra_sources: {arbeitnow: {enabled: true, max_pages: 5}} ->
    {"arbeitnow": {"max_pages": 5}}. Non-mapping values contribute {}.
    """
    out: dict[str, dict] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        name = str(k).strip().lower()
        if not name or not isinstance(v, dict):
            continue
        opts = {kk: vv for kk, vv in v.items()
                if str(kk).strip().lower() != "enabled"}
        if opts:
            out[name] = opts
    return out


def load_config(path: str | Path) -> Config:
    p = Path(path)
    data = _read_raw(p)

    def _str_list(key: str) -> list[str]:
        v = data.get(key, [])
        if v is None:
            return []
        if isinstance(v, str):
            return [v]
        return [str(x) for x in v]

    default_sites = _str_list("jobspy_sites") or ["linkedin"]
    return Config(
        search_terms=_str_list("search_terms"),
        locations=_parse_locations(data.get("locations", []), default_sites),
        hours_old=int(data.get("hours_old", 72)),
        results_wanted=int(data.get("results_wanted", 50)),
        jobspy_sites=default_sites,
        greenhouse_companies=_str_list("greenhouse_companies"),
        lever_companies=_str_list("lever_companies"),
        ashby_companies=_str_list("ashby_companies"),
        db_path=str(data.get("db_path", "jobs.db")),
        retention_days=int(data.get("retention_days", 3)),
        telegram_top_n=int(data.get("telegram_top_n", 10)),
        strict_filtering=bool(data.get("strict_filtering", True)),
        role_categories=_parse_role_categories(data.get("role_categories", [])),
        seniority=_parse_seniority(data.get("seniority_filter", {})),
        extra_sources=_parse_extra_sources(data.get("extra_sources", {})),
        extra_source_opts=_parse_extra_source_opts(data.get("extra_sources", {})),
        sections=_parse_sections(data, ["crawler", "nordictechjobs",
                                        "englishjobs", "jobberman",
                                        "myjobmag", "language_gate",
                                        "remote_scope"]),
    )
