"""Stage 3: title, tags and categories for a video, computed locally (no paid API).

- The title is the first line of the caption, without hashtags.
- Tags are the post's own hashtags, normalised for Obsidian.
- Categories come from the same bge-m3 vector used for search: the video's text is
  compared with each category's "name: definition" in categories.py. The best match
  is always the primary category; the next two are added only when they score nearly
  as high.
"""

import re
import threading
import unicodedata

import numpy as np

from . import embed
from .categories import CATEGORIES, CATEGORY_NAMES

TITLE_MAX_CHARS = 80
MAX_TAGS = 8
MAX_CATEGORIES = 3
TRANSCRIPT_CHARS = 2000  # how much of the transcript goes into the embedding

# An extra category must score within EXTRA_MARGIN of the best one and at least EXTRA_FLOOR.
EXTRA_MARGIN = 0.03
EXTRA_FLOOR = 0.30

HASHTAG_RE = re.compile(r"#\w+", re.UNICODE)

_category_matrix: np.ndarray | None = None
_lock = threading.Lock()


# --- text helpers ---------------------------------------------------------------

def title_from_caption(caption: str | None) -> str | None:
    """First non-empty caption line with hashtags removed, cut to TITLE_MAX_CHARS at a word boundary."""
    for line in (caption or "").splitlines():
        text = " ".join(HASHTAG_RE.sub("", line).split())
        if not text:
            continue
        if len(text) > TITLE_MAX_CHARS:
            text = text[:TITLE_MAX_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
        return text
    return None


def normalize_tag(tag: str) -> str:
    """Lowercase ASCII kebab-case, which Obsidian accepts as a tag. Purely numeric tags aren't valid there."""
    t = unicodedata.normalize("NFKD", tag).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9]+", "-", t.lower().lstrip("#")).strip("-")
    return "" if t.isdigit() else t


def tags_from_hashtags(hashtags: list[str] | None) -> list[str]:
    tags = (normalize_tag(h) for h in hashtags or [])
    return list(dict.fromkeys(t for t in tags if t))[:MAX_TAGS]


def embedding_text(caption: str | None, hashtags: list[str] | None, transcript: str | None) -> str:
    """What gets embedded for a video, for both search and categorisation."""
    parts = [
        (caption or "").strip(),
        " ".join(f"#{h}" for h in hashtags or []),
        (transcript or "").strip()[:TRANSCRIPT_CHARS],
    ]
    return "\n".join(p for p in parts if p)


SENTENCE_END_RE = re.compile(r"(?<=[.!?…])\s+")


def chunk_text(text: str | None, *, size: int, overlap: int = 100, max_chunks: int = 8, whole_window: int = TRANSCRIPT_CHARS) -> list[str]:
    """Split a long transcript into parts of about `size` characters, at sentence ends.

    Neighbouring parts share the last sentence(s) up to `overlap` characters, so an idea that
    straddles a boundary is whole in one of them. Returns [] when chunking is off (size <= 0) or
    the text already fits in the whole-video fingerprint, and at most `max_chunks` parts.
    """
    text = (text or "").strip()
    if size <= 0 or len(text) <= whole_window:
        return []
    pieces: list[str] = []
    for sentence in SENTENCE_END_RE.split(text):
        while len(sentence) > size:  # a run-on with no sentence end: cut it
            pieces.append(sentence[:size])
            sentence = sentence[size:]
        if sentence:
            pieces.append(sentence)

    chunks: list[str] = []
    current: list[str] = []
    length = 0
    for piece in pieces:
        if current and length + 1 + len(piece) > size:
            chunks.append(" ".join(current))
            if len(chunks) >= max_chunks:
                return chunks
            tail: list[str] = []
            tail_len = 0
            for earlier in reversed(current):  # carry the last sentence(s) over as overlap
                if tail_len + len(earlier) > overlap:
                    break
                tail.insert(0, earlier)
                tail_len += len(earlier) + 1
            current, length = tail, tail_len
        current.append(piece)
        length += len(piece) + 1
    if current and len(chunks) < max_chunks:
        chunks.append(" ".join(current))
    return chunks


# --- categories -----------------------------------------------------------------

def pick_categories(scores: np.ndarray) -> list[str]:
    """Choose 1-3 category names from similarity scores aligned with CATEGORY_NAMES."""
    order = np.argsort(-scores)
    best = float(scores[order[0]])
    picked = [CATEGORY_NAMES[order[0]]]
    for i in order[1:MAX_CATEGORIES]:
        score = float(scores[i])
        if score >= EXTRA_FLOOR and best - score <= EXTRA_MARGIN:
            picked.append(CATEGORY_NAMES[i])
    return picked


def _get_category_matrix() -> np.ndarray:
    global _category_matrix
    with _lock:
        if _category_matrix is None:
            _category_matrix = np.stack(embed.embed([f"{name}: {text}" for name, text in CATEGORIES.items()]))
        return _category_matrix


def classify(vector: np.ndarray) -> list[str]:
    """Categories for a video's (normalised) embedding."""
    return pick_categories(_get_category_matrix() @ vector)
