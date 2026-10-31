"""Group finished videos into topics (themes), using the same fingerprints as search.

Videos whose fingerprints are close are put in one group (average-linkage clustering on cosine
distance). A group with at least TOPIC_MIN_SIZE videos becomes a topic, named after the hashtags
that set it apart from the rest of the archive. The rest stay ungrouped. Everything runs locally.

    python -m tiktok_archive.topics --preview     # show the topics, change nothing
    python -m tiktok_archive.topics               # replace the stored topics

Topics are rebuilt from scratch each time, so run it again after processing more videos, then
write the notes again to get the hub notes in <vault>/_Topics/.
"""

import argparse
import math
from collections import Counter

import numpy as np

from . import db
from .config import get_settings, utf8_console

# Hashtags that are on half of TikTok and say nothing about the topic.
GENERIC_TAGS = frozenset({
    "fyp", "foryou", "foryoupage", "fy", "viral", "trending", "tiktok", "xyzbca", "parati", "paratii",
    "explore", "explorepage", "learnontiktok", "tiktokpartner", "capcut",
})
NAME_TAGS = 3


def cluster(vectors: np.ndarray, *, similarity: float, min_size: int) -> list[list[int]]:
    """Indices of the vectors (rows, unit length) grouped into clusters, biggest first.

    Groups merge while their average similarity is at least `similarity`; groups smaller than
    `min_size` are left out.
    """
    n = len(vectors)
    if n < max(2, min_size):
        return []
    from sklearn.cluster import AgglomerativeClustering  # imported here: it is slow to import

    distance = np.clip(1.0 - vectors @ vectors.T, 0.0, 2.0).astype(np.float32)
    np.fill_diagonal(distance, 0.0)
    labels = AgglomerativeClustering(
        n_clusters=None, metric="precomputed", linkage="average", distance_threshold=1.0 - similarity,
    ).fit_predict(distance)
    groups: dict[int, list[int]] = {}
    for index, label in enumerate(labels):
        groups.setdefault(int(label), []).append(index)
    return sorted((g for g in groups.values() if len(g) >= min_size), key=lambda g: (-len(g), g[0]))


def order_by_centre(vectors: np.ndarray, members: list[int]) -> list[int]:
    """Members sorted from closest to the group's average fingerprint to farthest."""
    centre = vectors[members].mean(axis=0)
    centre /= np.linalg.norm(centre) or 1.0
    scores = vectors[members] @ centre
    return [members[i] for i in np.argsort(-scores, kind="stable")]


def name_topic(member_tags: list[list[str]], document_frequency: Counter, total: int, member_categories: list[list[str]]) -> str:
    """A name from the tags this group uses far more than the archive as a whole.

    score = (videos in the group with the tag) x log(total / videos anywhere with the tag), so a tag
    on every video scores 0. Needs a tag on at least 2 videos; falls back to the commonest category.
    """
    in_group = Counter(t for tags in member_tags for t in set(tags))
    scored = [
        (count * math.log((total + 1) / (document_frequency[tag] + 1)), tag)
        for tag, count in in_group.items() if count >= 2 and tag not in GENERIC_TAGS
    ]
    best = [tag for score, tag in sorted(scored, key=lambda x: (-x[0], x[1])) if score > 0][:NAME_TAGS]
    if best:
        return " · ".join(best)
    categories = Counter(c[0] for c in member_categories if c)
    return f"{categories.most_common(1)[0][0]} (mixed)" if categories else "Mixed"


def build_topics(rows: list[dict], *, similarity: float, min_size: int) -> tuple[list[dict], int]:
    """From topic_inputs rows: ([{name, members: [video_id...], titles: [...]}], number ungrouped)."""
    if not rows:
        return [], 0
    def as_array(e) -> np.ndarray:  # the database driver returns a pgvector Vector object
        return np.asarray(e.to_numpy() if hasattr(e, "to_numpy") else e, dtype=np.float32)

    vectors = np.stack([as_array(r["embedding"]) for r in rows])
    frequency = Counter(t for r in rows for t in set(r["tags"] or []))
    taken: set[str] = set()
    topics = []
    for group in cluster(vectors, similarity=similarity, min_size=min_size):
        ordered = order_by_centre(vectors, group)
        name = name_topic([rows[i]["tags"] or [] for i in ordered], frequency, len(rows), [rows[i]["categories"] or [] for i in ordered])
        unique, n = name, 2
        while unique.lower() in taken:
            unique, n = f"{name} ({n})", n + 1
        taken.add(unique.lower())
        topics.append({
            "name": unique,
            "members": [rows[i]["video_id"] for i in ordered],
            "titles": [rows[i]["title"] or f"TikTok {rows[i]['video_id']}" for i in ordered],
        })
    return topics, len(rows) - sum(len(t["members"]) for t in topics)


def main() -> None:
    utf8_console()
    s = get_settings()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--preview", action="store_true", help="show the topics without saving them")
    parser.add_argument("--similarity", type=float, default=s.topic_similarity, help=f"default {s.topic_similarity}")
    parser.add_argument("--min-size", type=int, default=s.topic_min_size, help=f"default {s.topic_min_size}")
    args = parser.parse_args()

    with db.connect() as conn:
        rows = db.topic_inputs(conn)
        print(f"{len(rows)} finished videos with fingerprints")
        topics, ungrouped = build_topics(rows, similarity=args.similarity, min_size=args.min_size)
        for t in topics:
            print(f"\n{t['name']}  ({len(t['members'])} videos)")
            for title in t["titles"][:3]:
                print(f"    {title[:90]}")
        print(f"\n{len(topics)} topics, {len(rows) - ungrouped} videos grouped, {ungrouped} left ungrouped")
        if args.preview:
            print("preview only: nothing saved")
        else:
            db.replace_topics(conn, topics)
            print("saved. Write the notes again (step 5) to get the topic hub notes.")


if __name__ == "__main__":
    main()
