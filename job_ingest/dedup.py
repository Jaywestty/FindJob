"""Deduplication via hash of title+company+location."""
from __future__ import annotations

import hashlib

from .models import Job


def _norm(s: object) -> str:
    return str(s or "").strip().lower()


def dedup_hash(job: Job) -> str:
    key = "|".join((_norm(job.title), _norm(job.company), _norm(job.location)))
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def dedupe_jobs(jobs: list[Job]) -> list[Job]:
    """Drop duplicates (first occurrence wins). Jobs without URL are still deduped."""
    seen: set[str] = set()
    out: list[Job] = []
    for job in jobs:
        h = dedup_hash(job)
        if h in seen:
            continue
        seen.add(h)
        out.append(job)
    return out
