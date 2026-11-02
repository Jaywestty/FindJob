"""Job match scoring package: embeddings + rules -> match_percentage."""
from .profile import MatchProfile, load_profile
from .scoring import rescore_all, score_job, score_texts

__all__ = ["MatchProfile", "load_profile", "rescore_all", "score_job", "score_texts"]
