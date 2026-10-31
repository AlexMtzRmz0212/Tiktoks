"""Local embeddings with sentence-transformers (BAAI/bge-m3 by default, 1024-d, multilingual).

Used for semantic search and for categorising videos (classify.py). The model
downloads to the Hugging Face cache on first use (about 2.3 GB).
"""

import threading

import numpy as np

from .config import EMBEDDING_DIM, get_settings

_model = None
_lock = threading.Lock()


def _get_model():
    global _model
    if _model is None:
        from sentence_transformers import SentenceTransformer

        s = get_settings()
        _model = SentenceTransformer(s.embedding_model, device=s.embedding_device)
        dim = _model.get_sentence_embedding_dimension()
        if dim != EMBEDDING_DIM:
            raise RuntimeError(
                f"{s.embedding_model} produces {dim}-d vectors but the database column is VECTOR({EMBEDDING_DIM}). "
                "Pick a matching model or add a migration that changes the column."
            )
    return _model


def is_loaded() -> bool:
    """Whether the model is already in memory (the first use takes several seconds)."""
    return _model is not None


def embed(texts: list[str]) -> list[np.ndarray]:
    with _lock:
        vectors = _get_model().encode(texts, normalize_embeddings=True, convert_to_numpy=True)
    return [v.astype(np.float32) for v in vectors]


def embed_one(text: str) -> np.ndarray:
    return embed([text])[0]
