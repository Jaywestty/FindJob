# JobTool

A personal, local-first job-hunting engine. It pulls postings from 20+ boards
across Nigeria, Europe, the Gulf and global-remote, keeps only the roles you
actually want, scores each one against your resume profile with embeddings
plus rule adjustments, and exposes everything through a local REST API you
can drive from any UI.

- **Single-user, localhost-only, SQLite-backed.** No auth, no server
  deployment, no third-party backend — that is a deliberate design, not an
  oversight.
- **Config over code.** Sources, filters, location groups, boost/penalty
  weights and retention all live in `config.yaml` / `profile.yaml`. Most
  behaviour changes never touch Python.

---

## What it does

1. **Ingest** — `ingest.py` fetches jobs from every configured source
   (JobSpy search combos, ATS boards, public APIs, polite crawlers).
2. **Gate** — before a row can reach the database it must pass the
   role-family filter, the seniority filter, the language gate and a
   remote-scope classification.
3. **Store** — deduped by URL into SQLite (`jobs.db`), with tracking columns
   (`applied`, `interview_status`, `notes`).
4. **Score** — `job_match` embeds your profile and every job description
   (fastembed, `BAAI/bge-small-en-v1.5`) and layers rule boosts/penalties on
   top of the cosine similarity → `match_percentage` (0–100).
5. **Serve** — a FastAPI app (`api.py`) on `http://127.0.0.1:8000` is the
   *only* component allowed to read/write the DB. Any UI (old or new) talks
   to the API, never to SQLite directly.
6. **Operate** — `run_daily.py` chains ingest → rescore → Telegram digest →
   purge of stale unapplied rows each day.

## Architecture

```
                    ┌──────────────────── sources (job_ingest/sources/)
search_terms ×      │  JobSpy: LinkedIn / Indeed / Glassdoor per location
locations           │  ATS boards: Greenhouse, Lever, Ashby (slug based)
                    │  Public APIs: RemoteOK, Remotive, Himalayas, Jobicy,
                    │               Arbeitnow, WorkingNomads, Python.org,
                    │               Hacker News "Who is Hiring"
                    │  Crawlers: NordicTechJobs, EnglishJobs, Jobberman,
                    │            MyJobMag (robots.txt aware, rate-limited)
                                    │ fetch
                                    ▼
        ingest.py ──► dedup by URL ──► gates ──► SQLite jobs.db
                                               (auto-migrated schema)
                                    │
                                    ▼
        job_match/scoring.py (embeddings + rules) ──► match_percentage
                                    │
                                    ▼
                          FastAPI  api.py :8000   ◄── sole DB consumer
                                    │
                    ┌───────────────┼────────────────────┐
                    ▼               ▼                    ▼
             board.py (legacy   run_daily.py        any client you
             Streamlit UI —     daily pipeline      build (web/mobile)
             being replaced)    + Telegram digest
```

## Repo layout

| Path | What it is |
| --- | --- |
| `ingest.py` | CLI entry point: runs every source, filters, writes SQLite |
| `score.py` | CLI: score (or re-score) jobs against `profile.yaml` |
| `run_daily.py` | Daily orchestrator: ingest → rescore → alert → purge |
| `backfill_purge.py` | One-off: retro-apply new filters to existing rows (dry-run by default) |
| `setup_telegram.py` | One-off: verify Telegram credentials with a test message |
| `api.py` | FastAPI app — the database contract for all clients |
| `board.py` | **Legacy** Streamlit UI (see the frontend section) |
| `.streamlit/` | Legacy Streamlit theme |
| `config.yaml` | Sources, locations, filters, gates, retention |
| `profile.yaml` | Resume profile text + scoring weights |
| `job_ingest/` | Ingestion package: config, DB, dedup, filters, gates, tracking, crawl layer |
| `job_ingest/sources/` | One module per job source |
| `job_match/` | Matching package: embeddings, profile, rules, scoring, score store |
| `tests/` | Network-free `unittest` tests (pure logic) |
| `jobs.db` | Local SQLite database (gitignored, recreated on first run) |

## Quickstart

Requirements: **Python 3.12** and [uv](https://docs.astral.sh/uv/).

```bash
# 1. install locked dependencies into an isolated .venv
uv sync

# 2. run a full ingest (creates jobs.db automatically on first run)
uv run ingest.py

# 3. score everything against your profile
uv run score.py

# 4. start the local API (never expose this beyond localhost)
uv run uvicorn api:app --host 127.0.0.1 --port 8000

# 5. open the legacy UI while the API is running
uv run streamlit run board.py
```

Interactive API docs are served by FastAPI itself at
`http://127.0.0.1:8000/docs` (Swagger) and `/openapi.json`.

### Environment variables

Only Telegram needs credentials, and only for the daily digest. Create a
`.env` file next to `run_daily.py` (it is gitignored):

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789
```

Run `uv run setup_telegram.py` once to validate them. Without them the daily
run still works — it just skips the digest.

### Daily automation

```bash
uv run run_daily.py
```

Ingest → score only the new rows → send a Telegram digest of this run's top
matches → delete unapplied rows older than `retention_days` → print
`fetched=… inserted=… purged=… alert_sent=…`. Hook it into Windows Task
Scheduler / cron as you like.

## Configuration

### `config.yaml` (ingestion behaviour)

| Key | What it controls |
| --- | --- |
| `search_terms` × `locations` | JobSpy search combos; each location pins its valid `sites` and `country_indeed` (e.g. zip_recruiter is US/Canada-only, Glassdoor unsupported in Portugal) |
| `hours_old`, `results_wanted` | Freshness window (72h) and per-combo result cap (50) |
| `greenhouse_companies` / `lever_companies` / `ashby_companies` | ATS board slugs; dead slugs (404) are auto-dropped each run by `slugcheck` |
| `extra_sources` | Toggle any of the API/RSS/crawler sources without code changes |
| `crawler` | Shared polite-crawler policy: robots.txt crawl-delay honouring, per-run detail-fetch cap (150), User-Agent |
| `strict_filtering`, `role_categories` | Pre-insert relevance gate (currently `ai_ml` and `backend` families) |
| `seniority_filter` | Rejects senior/staff/principal/lead/… titles and interns; `max_years_experience: 5` |
| `language_gate` | Rejects jobs requiring German/Swedish/Dutch/French fluency; min share of English stopwords in the description |
| `remote_scope` | Classifies remote postings as `worldwide` / `restricted` from the point of view of Lagos, Nigeria |
| `retention_days` | Daily purge deletes unapplied rows older than this (applied rows are **never** deleted) |
| `db_path` | SQLite file location (`jobs.db`) |

### `profile.yaml` (matching behaviour)

- `profile_text` — embedded once and cosine-compared against every job
  description as the base score.
- `target_roles`, `boost_keywords` — rule boosts layered on top.
- Boost/penalty weights — `title_boost`, `keyword_boost`, experience
  penalty for posts demanding > 5 years, onsite-location penalty outside
  `home_location` ("Lagos"), remote-scope adjustments, optional
  visa/relocation block.
- `embedding_model` — fastembed model id (`BAAI/bge-small-en-v1.5`).

Scoring formula (`job_match/scoring.py`):

```
final = clamp(base + title_boost + keyword_boost + visa_boost
              + worldwide_boost − experience_penalty − location_penalty
              − restricted_penalty, 0..1) × 100
```

## REST API (the contract for every client)

Base URL `http://127.0.0.1:8000`, no auth. Background actions (ingest,
rescore) return `202` immediately and are tracked in `GET /health` — poll
that to know when they finish.

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/jobs` | List/search jobs (ordered by match % desc) |
| `GET` | `/jobs/{id}` | One job including full `description` |
| `PATCH` | `/jobs/{id}` | Application tracking update |
| `GET` | `/filter-options` | Distinct `sources`, valid `regions` and `interview_statuses` |
| `GET` | `/health` | Row counts, last ingest, background-task states |
| `GET` | `/stats/summary` | Dashboard payload (funnel, timeline, score bins) |
| `POST` | `/ingest` | Run ingestion in background (`202`; `409` if already running) |
| `POST` | `/rescore?only_unscored=` | Re-score in background (`202`; `409`) |

### `GET /jobs?min_score=&source=&region=&applied=&date_from=&date_to=&limit=&offset=`

```json
{
  "total": 412, "limit": 100, "offset": 0,
  "jobs": [
    {
      "id": 107,
      "title": "Backend Engineer",
      "company": "Stripe",
      "location": "Remote",
      "region": "global",
      "remote_scope": "worldwide",
      "url": "https://boards.greenhouse.io/stripe/jobs/4001",
      "posted_date": "2026-11-01T09:00:00+01:00",
      "source": "greenhouse:stripe",
      "is_remote": true,
      "salary_min": null,
      "salary_max": null,
      "match_percentage": 86.3,
      "applied": false,
      "applied_date": null,
      "interview_status": null,
      "notes": null
    }
  ]
}
```

- `id` is the SQLite `rowid` — use it for `/jobs/{id}` and `PATCH`.
- `region` values: `nigeria` | `europe` | `gulf` | `global` | `nordics` |
  `other` (derived from the location string when the row has none stored).
- `remote_scope`: `worldwide` | `restricted` | `null`.
- `match_percentage` is `null` until scored; `salary_min/max` are often null;
  `posted_date` is best-effort ISO and may be a raw text value from a
  quirky source — render defensively.

### `PATCH /jobs/{id}` — application tracking

Body (omit fields you don't change):

```json
{ "applied": true, "interview_status": "interview_scheduled", "notes": "Referred by Ada" }
```

- `applied_date` is stamped server-side; never send it.
- `interview_status` must be one of `pending`, `no_response`, `rejected`,
  `interview_scheduled`, `interview_completed`, `offer` (or `null` to clear)
  — invalid values return `422`, unknown ids `404`.
- Returns the updated job (with `description`).

### `GET /stats/summary`

```json
{
  "funnel": { "total_fetched": 5200, "total_kept": 412, "applied_count": 38,
               "interview_count": 5, "offer_count": 1 },
  "interview_breakdown": { "rejected": 10, "no_response": 22, "pending": 3 },
  "applications_timeline": [{ "date": "2026-10-04", "count": 2 }],
  "score_distribution": [{ "bin": "80-89", "count": 14 }],
  "response_rate": 13.2,
  "days_since_last_application": 2
}
```

## Frontend / mobile developer guide

> **You are replacing the Streamlit UI.** This repo currently ships a working
> but throwaway Streamlit prototype (`board.py` + `.streamlit/`). It works
> only as a *reference implementation* showing which features the product
> needs — do not extend, redesign or build on it. Your deliverable is a
> proper frontend/web app (or mobile app) consuming the API above; the legacy
> files will be dropped once your replacement covers its features.

Pick any stack — React/Next.js, Vue, Svelte, Flutter, React Native,
anything. The backend is plain JSON REST, so client-side choice is entirely
open. Recommendation: generate a typed API client from
`http://127.0.0.1:8000/openapi.json` (openapi-typescript, orval, autoRest,
regen for Flutter, …) rather than hand-writing request/response types (openapi-typescript, orval,
or Flutter's `openapi_generator` all work against `/openapi.json`).

### Getting set up

1. Clone the repo, run `uv sync`, start the API exactly as in
   [Quickstart](#quickstart). That is the entire backend story.
2. Requests go to `http://127.0.0.1:8000` with no auth; if your app is
   served from a different origin (Vite/Next dev server), CORS must be
   enabled — currently it is **not**. Adding `CORSMiddleware` in `api.py` is
   a small, expected change; coordinate in a PR so both sides see it.
3. Never import or read `jobs.db` from the client layer. Everything flows
   through the REST API (this is enforced by design in the current UI too).

### Feature parity checklist (from the legacy UI)

- **Jobs list** with filters: minimum match %, sources (multi-select),
  regions (multi-select), posted-date range, applied/unapplied, limit — all
  passed as query params to `GET /jobs`; fetch `GET /filter-options` for the
  exact values; show match % badge, source/region/date caption, remote-scope
  tag, link out to the posting URL.
- **Job detail**: full description via `GET /jobs/{id}`.
- **Applied tracker**: tick a job as applied, then track `interview_status`
  (dropdown of the six valid values) and free-text `notes` via `PATCH`.
- **Dashboard** from `GET /stats/summary`: pipeline funnel
  (fetched → kept → applied → interview → offer), applications-by-status
  breakdown, last-30-day application timeline, match-score distribution,
  response rate, days-since-last-application.
- **Actions**: buttons for "run ingestion" and "rescore" (`POST`
  endpoints, `202`), then show progress by polling `GET /health`
  (`tasks.ingest.state` / `tasks.rescore.state`).

### Error/status semantics

| Code | Meaning |
| --- | --- |
| `202` | Background job accepted — poll `/health` for state |
| `404` | No job with that `id` |
| `409` | Ingest/rescore already running |
| `422` | Invalid `interview_status` (validation) |

## Extending the backend

- **Add a job source**: create `job_ingest/sources/<name>.py` exposing
  `fetch_<name>_jobs(...) -> list[Job]` (reuse the normalized `Job` model in
  `job_ingest/models.py`), register it in `ingest.py`'s fetcher table, add a
  toggle/section in `config.yaml`. Follow the existing try/except pattern —
  a failing source must never crash the run. If it crawls pages, plug into
  `job_ingest/crawler.py` (robots.txt + crawl-delay + shared detail cap).
- **Add a filter / score rule**: filters live in `job_ingest/filters.py`
  and gates in `language.py` / `remote_scope.py`, all seeded from
  `config.yaml`; score rules in `job_match/rules.py` seeded from
  `profile.yaml`. Prefer new YAML knobs over editing constants.
- **Schema changes**: `job_ingest/db.py` auto-migrates by checking
  `PRAGMA table_info` and `ALTER TABLE ... ADD COLUMN` on startup — follow
  the same pattern (and the same in `job_ingest/tracking.py`).

## Testing

Existing tests are network-free `unittest` suites (fast, no HTTP):

```bash
uv run python -m unittest discover -s tests -p "test_*.py" -v
```

Add pure-logic tests (regexes, parsers, scoring rules) in this style; keep
anything touching the network as manual/ad-hoc scripts, not in the test
suite.

## Contributing conventions

- Python 3.12 only (`requires-python = "==3.12.*"`), dependencies managed
  and locked by uv — after adding one, run `uv lock` and commit `uv.lock`.
- Match the local style: `from __future__ import annotations`, full type
  hints, a module docstring explaining *why* at the top of each file, and
  comments tuned to leaving the next reader fewer questions than you had.
- One feature per branch off `main`, PR with a short description of the
  behaviour change (and config knobs, if any).
- The tool is localhost-only by design: don't add auth, don't add remote
  hosting, don't commit personal data (`.env`, `jobs.db`, logs are ignored).

## Housekeeping notes

- `retention_days: 3` purges unapplied rows daily; application history
  (`applied = 1`) is permanent. `backfill_purge.py --confirm` applies
  *changed filter rules* to the existing table (dry-run without the flag).
- `ingestion_runs` records fetched/kept totals per run — so funnel numbers
  are directional, not an exact conversion rate (duplicates across runs are
  counted in `fetched`).
- The crawler layer honours robots.txt `Crawl-delay`, sends an honest
  User-Agent and caps detail-page fetches per run; keep it that way if you
  add sources.
