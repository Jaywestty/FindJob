"""python-jobspy source: per-region sites (LinkedIn, Indeed, Glassdoor, Bayt, ...).

Each location object carries its own valid ``sites`` list and its own
``country_indeed`` value. ZipRecruiter is US/Canada-only and is never sent
for other regions; Indeed/Glassdoor require a country and are skipped when
the location's ``country_indeed`` is null (e.g. Nigeria). The
``country_indeed`` argument is omitted from the ``scrape_jobs`` call entirely
when it is null (Bayt/LinkedIn-only locations).

LinkedIn Remote country failures (Part 1 fix): jobspy's LinkedIn scraper
parses each returned job's "City, State, Country" string through
``Country.from_string`` (jobspy/linkedin/__init__.py ``_get_location``),
which raises ``ValueError("Invalid country string: '<country>'")`` for any
country outside jobspy's ``Country`` enum (seen: iceland, bosnia and
herzegovina, lebanon on the generic Remote / Remote Europe / Remote Middle
East queries). The exception escapes the per-site thread and aborts the
whole ``scrape_jobs`` call, losing every result. Fix approach used here: a
small guarded runtime shim on ``Country.from_string`` that returns the raw
country string (``Location.country`` accepts ``Country | str``) instead of
raising, applied only when the installed python-jobspy version matches the
tested one (1.1.82). The per-country-queries alternative (one query per
supported country with is_remote=True) was rejected: it would multiply the
three Remote combos into dozens of LinkedIn calls. If the installed version
ever differs, the shim is skipped with a warning and the original behaviour
(abort + no retry) applies.

Bayt: upstream returns HTTP 403 on every request, so the source is disabled
via config (bayt removed from config.yaml site lists). Any lingering bayt
entry is filtered here with a single one-line notice per run, and any bayt
HTTP 403 is logged only once per run instead of once per attempt/combo.

Glassdoor: some locations fail with HTTP 400 / "location not parsed"
(upstream logs "Glassdoor: location not parsed" and returns no rows for
that site). The location is logged with the error, and Glassdoor is then
skipped for that location for the rest of the run.
"""
from __future__ import annotations

import logging

import pandas as pd
import requests

from ..models import Job, parse_posted_date
from ..retry import with_retry

log = logging.getLogger(__name__)

# Indeed/Glassdoor need an explicit country; LinkedIn/Bayt ignore it.
_COUNTRY_REQUIRED_SITES = {"indeed", "glassdoor"}

# python-jobspy version the country shim was tested against.
_SHIM_TESTED_VERSION = "1.1.82"
_shim_state: str = "pending"  # pending | applied | skipped

# Locations whose Glassdoor lookup already failed once this run: Glassdoor
# is dropped for them on subsequent combos (reset per process == per run).
_GLASSDOOR_SKIP_LOCS: set[str] = set()
# Bayt 403 deduplication: one line per run, not one per attempt/combo.
_BAYT_403_LOGGED = False
_BAYT_DISABLED_LOGGED = False


def _jobspy_version() -> str:
    try:
        from importlib.metadata import version as _pkg_version
        return _pkg_version("python-jobspy")
    except Exception:  # noqa: BLE001
        return "unknown"


def _ensure_country_shim() -> None:
    """Patch Country.from_string to be non-fatal on unknown countries.

    Only applied when the installed python-jobspy version matches the
    tested one; otherwise left alone with a warning.
    """
    global _shim_state
    if _shim_state != "pending":
        return
    ver = _jobspy_version()
    if ver != _SHIM_TESTED_VERSION:
        _shim_state = "skipped"
        log.warning(
            "jobspy country shim SKIPPED: installed python-jobspy %s != "
            "tested %s; LinkedIn Remote combos may abort on unsupported "
            "countries (no retry will be attempted)",
            ver, _SHIM_TESTED_VERSION,
        )
        return
    try:
        from jobspy.model import Country as _Country
    except Exception as exc:  # noqa: BLE001
        _shim_state = "skipped"
        log.warning("jobspy country shim SKIPPED: cannot import Country: %s", exc)
        return
    if getattr(_Country.from_string, "__jobtool_shimmed__", False):
        _shim_state = "applied"
        return
    _original = _Country.from_string.__func__  # classmethod underlying fn

    @classmethod  # type: ignore[misc]
    def _tolerant_from_string(cls, country_str: str):  # noqa: ANN001,ANN202
        try:
            return _original(cls, country_str)
        except ValueError as exc:
            if "Invalid country string" not in str(exc):
                raise
            log.warning(
                "jobspy country shim: unsupported country %r kept as raw "
                "string instead of aborting the scrape", country_str,
            )
            return str(country_str).strip().lower() or None

    _tolerant_from_string.__jobtool_shimmed__ = True  # type: ignore[attr-defined]
    _Country.from_string = _tolerant_from_string
    _shim_state = "applied"
    log.info(
        "jobspy country shim APPLIED (python-jobspy %s): unsupported "
        "LinkedIn job countries are kept as raw strings, non-fatal",
        ver,
    )


def _to_float(v: object) -> float | None:
    try:
        if v is None:
            return None
        s = str(v).strip()
        if not s or s.lower() == "nan":
            return None
        return float(s.replace(",", ""))
    except (ValueError, TypeError):
        return None


def _row_to_job(row: pd.Series) -> Job | None:
    def _get(*keys: str, default: str = "") -> str:
        for k in keys:
            if k in row and pd.notna(row[k]):
                return str(row[k])
        return default

    url = _get("job_url", "job_url_direct")
    if not url:
        return None
    is_remote_raw = row.get("is_remote", False)
    if pd.isna(is_remote_raw):
        is_remote = False
    elif isinstance(is_remote_raw, str):
        is_remote = is_remote_raw.strip().lower() in ("true", "1", "yes", "remote")
    else:
        is_remote = bool(is_remote_raw)

    job = Job(
        title=_get("title"),
        company=_get("company"),
        description=_get("description"),
        location=_get("location"),
        url=url,
        posted_date=parse_posted_date(row.get("date_posted")),
        source=_get("site", default="jobspy") or "jobspy",
        is_remote=is_remote,
        salary_min=_to_float(row.get("min_amount")),
        salary_max=_to_float(row.get("max_amount")),
    )
    return job.clean()


def _effective_sites(sites: list[str], region: str, location: str) -> list[str]:
    """Enforce region validity. Never sends zip_recruiter outside north_america."""
    global _BAYT_DISABLED_LOGGED
    cleaned = [s.strip().lower() for s in sites if s and s.strip()]
    if "zip_recruiter" in cleaned and region.strip().lower() != "north_america":
        log.warning(
            "Skipping zip_recruiter for %r (region %r: US/Canada only)",
            location, region,
        )
        cleaned = [s for s in cleaned if s != "zip_recruiter"]
    if "bayt" in cleaned and not _BAYT_DISABLED_LOGGED:
        # Bayt is disabled (upstream 403 on every request); one line per run.
        log.info(
            "Bayt disabled (upstream HTTP 403 on every request); "
            "skipping bayt for this run (config keeps the code path)"
        )
        _BAYT_DISABLED_LOGGED = True
    cleaned = [s for s in cleaned if s != "bayt"]
    # Skip Glassdoor for locations that already failed once this run.
    if "glassdoor" in cleaned and location in _GLASSDOOR_SKIP_LOCS:
        log.info(
            "Skipping glassdoor for %r (already failed once this run)",
            location,
        )
        cleaned = [s for s in cleaned if s != "glassdoor"]
    return cleaned


def _is_glassdoor_location_failure(exc: BaseException) -> bool:
    msg = str(exc).lower()
    if "glassdoor" in msg and ("location" in msg or "400" in msg):
        return True
    if isinstance(exc, ValueError) and "not found on glassdoor" in msg:
        return True
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 400 and "glassdoor" in msg:
        return True
    return False


def _note_glassdoor_failure(location: str, detail: str) -> None:
    if location not in _GLASSDOOR_SKIP_LOCS:
        _GLASSDOOR_SKIP_LOCS.add(location)
        log.error(
            "Glassdoor failed for location %r (%s); skipping Glassdoor "
            "for this location for the rest of the run",
            location, detail,
        )


@with_retry(max_attempts=3, backoff_seconds=2.0, source_name="jobspy")
def _scrape_once(search_term: str, location: str, hours_old: int,
                 results_wanted: int, sites: list[str],
                 country_indeed: str | None = None,
                 region: str = "global") -> pd.DataFrame:
    from jobspy import scrape_jobs  # lazy import so ATS-only runs don't need it

    _ensure_country_shim()
    sites = _effective_sites(sites, region, location)
    if not sites:
        raise ValueError(
            f"No valid JobSpy sites for location {location!r} (region {region!r})"
        )
    # Indeed/Glassdoor require a country; drop them when country is null
    # (e.g. Nigeria) instead of falling back to a wrong default.
    if country_indeed is None:
        dropped = [s for s in sites if s in _COUNTRY_REQUIRED_SITES]
        if dropped:
            log.warning(
                "Skipping %s for %r: country_indeed is null",
                ", ".join(dropped), location,
            )
        sites = [s for s in sites if s not in _COUNTRY_REQUIRED_SITES]
        if not sites:
            raise ValueError(
                f"No valid JobSpy sites for location {location!r}: "
                "country_indeed is null"
            )

    kwargs: dict = dict(
        site_name=sites,  # type: ignore[dict-item]
        search_term=search_term,
        location=location,
        hours_old=hours_old,
        results_wanted=results_wanted,
    )
    # Omit country_indeed entirely when null (LinkedIn/Bayt-only locations).
    if country_indeed is not None:
        kwargs["country_indeed"] = country_indeed
    try:
        return scrape_jobs(**kwargs)
    except Exception as exc:
        # Bayt 403: one line per run, not one per attempt.
        global _BAYT_403_LOGGED
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status == 403 and "bayt" in [s.lower() for s in sites]:
            if not _BAYT_403_LOGGED:
                _BAYT_403_LOGGED = True
                log.error(
                    "Bayt HTTP 403 (disabled upstream); skipping bayt output "
                    "for this run"
                )
            raise
        raise


def fetch_jobspy_jobs(search_term: str, location: str, hours_old: int = 72,
                      results_wanted: int = 50,
                      sites: list[str] | None = None,
                      country_indeed: str | None = None,
                      region: str = "global") -> list[Job]:
    """Scrape one (search_term, location) pair using that location's own
    valid ``sites`` list and ``country_indeed``. Failures raise (caller logs
    per-source)."""
    global _BAYT_403_LOGGED
    sites = list(sites) if sites else ["linkedin"]
    try:
        df = _scrape_once(search_term, location, hours_old, results_wanted,
                           sites, country_indeed, region)
    except ImportError:
        raise
    except Exception as exc:
        if _is_glassdoor_location_failure(exc):
            # Log with the location, remember the skip, and salvage the combo
            # by retrying once without glassdoor so LinkedIn/Indeed rows live.
            _note_glassdoor_failure(location, f"{type(exc).__name__}: {exc}")
            remaining = [s for s in sites
                         if s.strip().lower() not in ("glassdoor", "bayt")]
            if remaining and "glassdoor" in [s.strip().lower() for s in sites]:
                log.info("Retrying %r @ %r without glassdoor", search_term, location)
                try:
                    df = _scrape_once(search_term, location, hours_old,
                                       results_wanted, remaining,
                                       country_indeed, region)
                except Exception as exc2:  # noqa: BLE001
                    log.error("jobspy scrape failed for %r @ %r: %s",
                              search_term, location, exc2)
                    raise
            else:
                log.error("jobspy scrape failed for %r @ %r: %s",
                          search_term, location, exc)
                raise
        else:
            if isinstance(exc, requests.HTTPError):
                status = getattr(getattr(exc, "response", None),
                                 "status_code", None)
                if status == 403:
                    if not _BAYT_403_LOGGED:
                        _BAYT_403_LOGGED = True
                        log.error("JobSpy HTTP 403 for %r @ %r: %s",
                                  search_term, location, exc)
                    raise
            log.error("jobspy scrape failed for %r @ %r: %s", search_term, location, exc)
            raise
    if df is None or len(df) == 0:
        return []
    # Glassdoor silently returns zero rows on 400/location-not-parsed: detect
    # the missing site output, log with the location, and skip it henceforth.
    try:
        requested = {s.strip().lower() for s in sites}
        if "glassdoor" in requested and location not in _GLASSDOOR_SKIP_LOCS:
            present = set()
            if "site" in df.columns:
                present = {str(v).strip().lower() for v in df["site"].tolist()}
            if "glassdoor" not in present:
                _note_glassdoor_failure(
                    location, "no glassdoor rows returned "
                    "(likely HTTP 400 location-not-parsed upstream)")
    except Exception:  # noqa: BLE001
        pass
    jobs: list[Job] = []
    for _, row in df.iterrows():
        try:
            job = _row_to_job(row)
            if job is not None:
                jobs.append(job)
        except Exception as exc:  # noqa: BLE001
            log.warning("Skipping malformed jobspy row: %s", exc)
    return jobs
