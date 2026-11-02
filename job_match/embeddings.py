"""Local embeddings via FastEmbed (ONNX, no API key, no torch).

Model: BAAI/bge-small-en-v1.5 — 384-dim, normalized vectors, so cosine
similarity is a dot product. Chosen because it runs fully local and free
(no API costs/keys, descriptions stay on disk), needs only the lightweight
onnxruntime instead of a torch/CUDA stack, and is fast on CPU in batches.
"""
from __future__ import annotations

import logging
import re

import numpy as np

log = logging.getLogger(__name__)

# Bound input size to ~the model's 512-token context (~2000 chars): anything
# longer is truncated by the model anyway, so this only skips wasted work
# on very long (often HTML) descriptions.
MAX_CHARS = 2000
_STRIP_HTML = re.compile(r"<[^>]+>")


def clean_text(text: str | None) -> str:
    text = _STRIP_HTML.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:MAX_CHARS]


class Embedder:
    """Cached TextEmbedding instance (model downloads once, then offline)."""

    def __init__(self, model_name: str = "BAAI/bge-small-en-v1.5") -> None:
        from fastembed import TextEmbedding

        self.model = TextEmbedding(model_name=model_name)

    def embed(self, texts: list[str], batch_size: int = 64) -> np.ndarray:
        """Embed a batch -> (n, dim) float32 array. Empty input -> (0, dim)."""
        cleaned = [clean_text(t) for t in texts]
        if not cleaned:
            return np.zeros((0, 384), dtype=np.float32)
        vecs: list[np.ndarray] = []
        for i in range(0, len(cleaned), batch_size):
            chunk = cleaned[i:i + batch_size]
            # FastEmbed yields one vector per input (empty string -> zero vec).
            vecs.extend(np.asarray(v, dtype=np.float32) for v in self.model.embed(chunk))
        return np.stack(vecs) if vecs else np.zeros((0, 384), dtype=np.float32)


def cosine_similarities(profile_vec: np.ndarray, job_vecs: np.ndarray) -> np.ndarray:
    """Cosine of profile vs each job row, clipped to [0, 1] as base score."""
    if job_vecs.size == 0:
        return np.zeros(0, dtype=np.float64)
    p = np.asarray(profile_vec, dtype=np.float64).ravel()
    j = np.asarray(job_vecs, dtype=np.float64)
    denom = np.linalg.norm(p) * np.linalg.norm(j, axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        sims = (j @ p) / denom
    return np.clip(np.nan_to_num(sims, nan=0.0), 0.0, 1.0)
