#!/usr/bin/env python3
"""CLI entry point: uv run ingest.py [--config config.yaml] [--db jobs.db]

Setup: uv sync  (creates the isolated .venv and installs locked deps)
Run:   uv run ingest.py

Runs all sources (JobSpy + Greenhouse + Lever), dedupes, writes to SQLite,
and prints a summary count per source. Per-source failures are logged,
not fatal.
"""
from __future__ import annotations

import argparse
import itertools
import logging
import sys

from job_ingest.config import load_config
from job_ingest.db import init_db, record_ingestion_run, save_jobs
from job_ingest.dedup import dedup_hash, dedupe_jobs
from job_ingest.filters import job_passes_filters
from job_ingest.models import Job
from job_ingest.sources.arbeitnow import fetch_arbeitnow_jobs
from job_ingest.sources.ashby import fetch_ashby_jobs
from job_ingest.sources.englishjobs import fetch_englishjobs_jobs
from job_ingest.sources.greenhouse import fetch_greenhouse_jobs
from job_ingest.sources.hackernews import fetch_hackernews_jobs
from job_ingest.sources.himalayas import fetch_himalayas_jobs
from job_ingest.sources.jobberman import fetch_jobberman_jobs
from job_ingest.sources.myjobmag import fetch_myjobmag_jobs
from job_ingest.sources.nordictechjobs import fetch_nordictechjobs_jobs
from job_ingest.sources.pythonorg import fetch_pythonorg_jobs
from job_ingest.sources.workingnomads import fetch_workingnomads_jobs
from job_ingest.sources.jobicy import fetch_jobicy_jobs
from job_ingest.sources.jobspy_source import fetch_jobspy_jobs
from job_ingest.sources.lever import fetch_lever_jobs
from job_ingest.sources.remoteok import fetch_remoteok_jobs
from job_ingest.sources.remotive import fetch_remotive_jobs
from job_ingest.sources.weworkremotely import fetch_wwr_jobs

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ingest")


def _crawler_source_kwargs(cfg, name: str) -> dict:
    """Config -> kwargs for a crawler-based source fetch function.

    Merges the shared ``crawler`` block (detail cap, delay, UA) with the
    source's own section; section keys map 1:1 onto fetch-function params.
    """
    crawl = cfg.section("crawler")
    sec = cfg.section(name)
    kw: dict = dict(sec)
    kw.pop("enabled", None)
    kw.setdefault("max_detail_fetches",
                  int(crawl.get("max_detail_fetches_per_run", 150)))
    kw.setdefault("default_delay_seconds",
                  float(crawl.get("default_delay_seconds", 1.5)))
    ua = str(crawl.get("user_agent", "") or "").strip()
    if ua:
        kw.setdefault("user_agent", ua)
    return kw


def _int_opts(opts: dict, defaults: dict[str, int]) -> dict[str, int]:
    """Coerce per-source extra_sources options to ints with defaults."""
    out: dict[str, int] = {}
    for key, default in defaults.items():
        try:
            out[key] = int(opts.get(key, default))
        except (TypeError, ValueError):
            out[key] = default
    return out


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Ingest jobs from JobSpy + ATS boards.")
    p.add_argument("--config", default="config.yaml", help="Path to JSON/YAML config")
    p.add_argument("--db", default=None, help="Override SQLite path (default from config)")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_config(args.config)
    except Exception as exc:  # noqa: BLE001
        log.error("Failed to load config %s: %s", args.config, exc)
        return 2

    db_path = args.db or cfg.db_path
    init_db(db_path)

    per_source: dict[str, int] = {}
    failures: dict[str, str] = {}
    # (origin key, job) pairs so kept-counts stay attributable per source.
    all_jobs: list[tuple[str, Job]] = []

    # --- JobSpy: every search_term x location combo (per-location sites/country) ---
    combos = list(itertools.product(cfg.search_terms, cfg.locations))
    if combos:
        for term, loc in combos:
            key = f"jobspy:{term} @ {loc.name} [{loc.region}]"
            try:
                jobs = fetch_jobspy_jobs(
                    search_term=term,
                    location=loc.name,
                    hours_old=cfg.hours_old,
                    results_wanted=cfg.results_wanted,
                    sites=loc.sites,
                    country_indeed=loc.country_indeed,
                    region=loc.region,
                )
                per_source[key] = len(jobs)
                all_jobs.extend((key, j) for j in jobs)
            except Exception as exc:  # noqa: BLE001
                log.error("Source %s failed: %s: %s", key, type(exc).__name__, exc)
                per_source[key] = 0
                failures[key] = f"{type(exc).__name__}: {exc}"
    else:
        log.warning("No search_terms x locations configured; skipping JobSpy.")

    # --- ATS slug startup check: probe every configured Greenhouse/Lever/
    # --- Ashby slug once, log one summary line, skip dead slugs this run.
    from job_ingest.slugcheck import check_ats_slugs
    live_greenhouse, live_lever, live_ashby, _dead_slugs = check_ats_slugs(
        list(cfg.greenhouse_companies), list(cfg.lever_companies),
        list(cfg.ashby_companies),
    )

    # --- Greenhouse ---
    for company in live_greenhouse:
        key = f"greenhouse:{company}"
        try:
            jobs = fetch_greenhouse_jobs(company)
            per_source[key] = len(jobs)
            all_jobs.extend((key, j) for j in jobs)
        except Exception as exc:  # noqa: BLE001
            log.error("Source %s failed: %s: %s", key, type(exc).__name__, exc)
            per_source[key] = 0
            failures[key] = f"{type(exc).__name__}: {exc}"

    # --- Lever ---
    for company in live_lever:
        key = f"lever:{company}"
        try:
            jobs = fetch_lever_jobs(company)
            per_source[key] = len(jobs)
            all_jobs.extend((key, j) for j in jobs)
        except Exception as exc:  # noqa: BLE001
            log.error("Source %s failed: %s: %s", key, type(exc).__name__, exc)
            per_source[key] = 0
            failures[key] = f"{type(exc).__name__}: {exc}"

    # --- Ashby (slugs already liveness-checked above) ---
    for slug in live_ashby:
        key = f"ashby:{slug}"
        try:
            jobs = fetch_ashby_jobs(slug)
            per_source[key] = len(jobs)
            all_jobs.extend((key, j) for j in jobs)
        except Exception as exc:  # noqa: BLE001
            log.error("Source %s failed: %s: %s", key, type(exc).__name__, exc)
            per_source[key] = 0
            failures[key] = f"{type(exc).__name__}: {exc}"

    # --- Extra API/RSS sources (official public APIs/feeds, no scraping) ---
    # Same try/except pattern: per-source failure never crashes the run.
    # Remotive searches are built from the role_categories keywords so
    # coverage follows the filter config.
    role_searches = [cat.keywords[0] for cat in cfg.role_categories
                     if cat.keywords]
    extra_fetchers = {
        "remoteok": lambda: fetch_remoteok_jobs(),
        "remotive": lambda: fetch_remotive_jobs(role_searches),
        "himalayas": lambda: fetch_himalayas_jobs(),
        "jobicy": lambda: fetch_jobicy_jobs(),
        "weworkremotely": lambda: fetch_wwr_jobs(),
        "nordictechjobs": lambda: fetch_nordictechjobs_jobs(
            db_path, **_crawler_source_kwargs(cfg, "nordictechjobs")),
        "englishjobs": lambda: fetch_englishjobs_jobs(
            db_path, **_crawler_source_kwargs(cfg, "englishjobs")),
        "arbeitnow": lambda: fetch_arbeitnow_jobs(
            **_int_opts(cfg.source_opts("arbeitnow"), {"max_pages": 5})),
        "workingnomads": lambda: fetch_workingnomads_jobs(),
        "pythonorg": lambda: fetch_pythonorg_jobs(),
        "hackernews": lambda: fetch_hackernews_jobs(
            **_int_opts(cfg.source_opts("hackernews"), {"months_back": 2})),
        "jobberman": lambda: fetch_jobberman_jobs(
            db_path, **_crawler_source_kwargs(cfg, "jobberman")),
        "myjobmag": lambda: fetch_myjobmag_jobs(
            db_path, **_crawler_source_kwargs(cfg, "myjobmag")),
    }
    for name, fetch in extra_fetchers.items():
        if not cfg.extra_sources.get(name, True):
            log.info("Source %s disabled in config; skipping", name)
            continue
        try:
            jobs = fetch()
            per_source[name] = len(jobs)
            all_jobs.extend((name, j) for j in jobs)
        except Exception as exc:  # noqa: BLE001
            log.error("Source %s failed: %s: %s", name, type(exc).__name__, exc)
            per_source[name] = 0
            failures[name] = f"{type(exc).__name__}: {exc}"

    # --- Dedupe (origin-preserving) + language gate + remote scope +
    # --- role/seniority filter + persist. Filtered-out jobs never reach
    # the DB. Filtering runs after dedupe so counts read
    # fetched -> deduped -> kept.
    from collections import Counter
    from job_ingest.language import passes_language_gate
    from job_ingest.remote_scope import classify_remote_scope
    n_before = len(all_jobs)
    seen: set[str] = set()
    unique: list[tuple[str, Job]] = []
    for origin, job in all_jobs:
        h = dedup_hash(job)
        if h not in seen:
            seen.add(h)
            unique.append((origin, job))
    n_dupes = n_before - len(unique)
    lang_cfg = cfg.section("language_gate")
    scope_cfg = cfg.section("remote_scope")
    lang_removed: Counter = Counter()
    kept: list[tuple[str, Job]] = []
    for origin, job in unique:
        ok, reason = passes_language_gate(job.title, job.description,
                                          lang_cfg)
        if not ok:
            lang_removed[origin] += 1
            continue
        job.remote_scope = classify_remote_scope(
            job.title, job.location, job.description, job.is_remote,
            scope_cfg)
        if job_passes_filters(job, cfg):
            kept.append((origin, job))
    kept_counts = Counter(origin for origin, _ in kept)
    n_filtered = len(unique) - len(kept)
    if lang_removed:
        log.info("Language gate removed %d posting(s): %s",
                 sum(lang_removed.values()),
                 ", ".join(f"{src}={n}" for src, n in
                           sorted(lang_removed.items())))
    scope_counts = Counter((j.remote_scope or "onsite") for _, j in kept)
    log.info("Remote scope (kept jobs): %s", dict(sorted(scope_counts.items())))
    inserted, skipped = save_jobs([j for _, j in kept], db_path)

    # --- Summary (per source + per region) ---
    print("\n=== Ingestion summary ===")
    filter_tag = "" if cfg.strict_filtering else " (filtering OFF)"
    for source, count in per_source.items():
        flag = f"  FAILED: {failures[source]}" if source in failures else ""
        print(f"{source}: {count} fetched -> {kept_counts.get(source, 0)} kept{flag}")
    # Roll up JobSpy counts by region from the [region] suffix in keys.
    region_totals: dict[str, int] = {}
    for source, count in per_source.items():
        if source.startswith("jobspy:") and "[" in source and source.endswith("]"):
            region = source.rsplit("[", 1)[-1][:-1]
            region_totals[region] = region_totals.get(region, 0) + count
    if region_totals:
        print("--- by region (jobspy) ---")
        for region in sorted(region_totals):
            print(f"{region}: {region_totals[region]} fetched")
    print(f"Total fetched: {n_before}{filter_tag}")
    print(f"Duplicates removed (title+company+location): {n_dupes}")
    print(f"Filtered out (language/role/seniority): {n_filtered}")
    print(f"DB '{db_path}' table jobs: {inserted} inserted, {skipped} skipped (existing URL)")
    # One audit row per run for the dashboard funnel (fetched/kept as logged).
    try:
        record_ingestion_run(db_path, n_before, len(kept))
    except Exception as exc:  # noqa: BLE001
        log.warning("Could not record ingestion run: %s", exc)
    # Exposed for programmatic callers (e.g. run_daily.py); return code unchanged.
    main.last_stats = {"fetched": n_before, "inserted": inserted,
                       "skipped": skipped, "per_source": dict(per_source),
                       "kept": len(kept), "filtered_out": n_filtered,
                       "kept_per_source": dict(kept_counts),
                       "language_removed": dict(lang_removed),
                       "remote_scope": dict(scope_counts)}
    return 0


main.last_stats = {"fetched": 0, "inserted": 0, "skipped": 0, "per_source": {},
                   "kept": 0, "filtered_out": 0, "kept_per_source": {}}


if __name__ == "__main__":
    sys.exit(main())
