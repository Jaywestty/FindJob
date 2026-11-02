"""Combine embedding base score with rule adjustments -> match_percentage.

final = clamp(base + title_boost + keyword_boost + worldwide_boost
              + visa_boost - experience_penalty - location_penalty
              - restricted_penalty, 0, 1) * 100
"""
from __future__ import annotations

import logging

import numpy as np

from . import rules
from .embeddings import Embedder, clean_text, cosine_similarities
from .profile import MatchProfile, load_profile
from .store import fetch_jobs_for_scoring, save_scores

log = logging.getLogger(__name__)


def score_texts(profile_text: str, job_texts: list[str],
                embedder: Embedder | None = None) -> np.ndarray:
    """Base cosine scores for ad-hoc texts (no rules, no DB)."""
    emb = embedder or Embedder()
    p = emb.embed([profile_text])[0]
    return cosine_similarities(p, emb.embed(job_texts))


def score_job(base: float, title: str | None, description: str | None,
              location: str | None, is_remote: bool,
              profile: MatchProfile, remote_scope: str | None = "") -> dict:
    """Apply rules to one base score; returns breakdown + match_percentage."""
    t_boost, role = rules.title_boost(title, profile)
    k_boost, hits = rules.keyword_boost(description, profile)
    e_pen, years = rules.experience_penalty(description, profile)
    l_pen, loc_kind = rules.location_penalty(location, is_remote, profile)
    s_delta, s_kind = rules.remote_scope_adjustment(remote_scope, profile)
    v_boost, v_hit = rules.visa_boost(description, profile)
    final = min(1.0, max(0.0, base + t_boost + k_boost + v_boost
                         + (s_delta if s_delta > 0 else 0.0)
                         - e_pen - l_pen
                         - (-s_delta if s_delta < 0 else 0.0)))
    return {
        "base": round(float(base), 4),
        "title_boost": t_boost, "matched_role": role,
        "keyword_boost": round(float(k_boost), 4), "matched_keywords": hits,
        "experience_penalty": round(float(e_pen), 4), "required_years": years,
        "location_penalty": l_pen, "location_kind": loc_kind,
        "remote_scope_delta": round(float(s_delta), 4),
        "remote_scope_kind": s_kind,
        "visa_boost": v_boost, "visa_hit": v_hit,
        "match_percentage": round(final * 100, 1),
    }


def rescore_all(db_path: str, profile_path: str = "profile.yaml",
                only_unscored: bool = False,
                limit: int | None = None,
                batch_size: int = 64) -> dict:
    """Score jobs and store match_percentage. No ingestion involved.

    only_unscored=False re-scores every row (full refresh after profile edits).
    Returns stats: scored count, mean match, model name.
    """
    profile = load_profile(profile_path)
    if not profile.profile_text.strip():
        raise ValueError(f"profile_text is empty in {profile_path}")
    rows = fetch_jobs_for_scoring(db_path, only_unscored=only_unscored, limit=limit)
    if not rows:
        return {"scored": 0, "mean_match": 0.0, "model": profile.embedding_model}

    embedder = Embedder(model_name=profile.embedding_model)
    log.info("Embedding profile + %d job descriptions", len(rows))
    profile_vec = embedder.embed([profile.profile_text])[0]
    texts = [(r["description"] or "") if (r["description"] or "").strip()
             else (r["title"] or "") for r in rows]
    # Skip embedding cost for blank texts; they score base 0.
    blanks = {i for i, t in enumerate(texts) if not clean_text(t)}
    job_vecs = embedder.embed(texts, batch_size=batch_size)
    bases = cosine_similarities(profile_vec, job_vecs)
    for i in blanks:
        bases[i] = 0.0

    scores: dict[str, float] = {}
    for row, base in zip(rows, bases):
        detail = score_job(base, row["title"], row["description"],
                           row["location"], bool(row["is_remote"]), profile,
                           row.get("remote_scope", ""))
        scores[row["url"]] = detail["match_percentage"]
    updated = save_scores(db_path, scores)
    mean_match = round(float(np.mean(list(scores.values()))), 1) if scores else 0.0
    return {"scored": updated, "mean_match": mean_match,
            "model": profile.embedding_model}
