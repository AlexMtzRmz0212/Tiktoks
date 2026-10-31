"""Find videos by what they say: by meaning, by exact words, or both together.

    python -m tiktok_archive.search "how to negotiate a raise"
    python -m tiktok_archive.search "easy dinner" --category Food --status unprocessed -k 20
    python -m tiktok_archive.search "python automation" --tag python --since 2025-01-01
    python -m tiktok_archive.search "xlookup" --mode keywords

- meaning:  the query is turned into a fingerprint and compared with every video's fingerprint
            (works across wording and across English/Spanish).
- keywords: the query's words must appear in the video's title, caption, hashtags or transcript.
            Quotes ("exact phrase"), OR and -word work.
- both:     the two rankings are merged with Reciprocal Rank Fusion, so a video that ranks well in
            either list, and especially in both, comes first.
"""

import argparse
from datetime import datetime

from . import db, embed
from .categories import CATEGORY_NAMES
from .config import get_settings, utf8_console

MODES = ("both", "meaning", "keywords")
RRF_K = 60  # the usual constant: how quickly a lower rank stops mattering


def fuse_rankings(meaning_ids: list[str], keyword_ids: list[str], limit: int) -> list[tuple[str, float, str]]:
    """Merge two best-first id lists. Returns (video_id, score, matched) best first, matched being
    'meaning', 'keywords' or 'both'."""
    scores: dict[str, float] = {}
    sources: dict[str, set[str]] = {}
    for source, ids in (("meaning", meaning_ids), ("keywords", keyword_ids)):
        for rank, video_id in enumerate(ids, start=1):
            scores[video_id] = scores.get(video_id, 0.0) + 1.0 / (RRF_K + rank)
            sources.setdefault(video_id, set()).add(source)
    ordered = sorted(scores, key=lambda v: (-scores[v], v))[:limit]
    return [(v, scores[v], "both" if len(sources[v]) == 2 else next(iter(sources[v]))) for v in ordered]


def search(
    query: str,
    *,
    mode: str = "both",
    category: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    since: datetime | None = None,
    k: int = 10,
) -> list[dict]:
    """Best matches first. Each result has the video's summary fields plus score, matched and
    similarity (meaning similarity from 0 to 1, or None when only the keywords matched) and
    part (0 when the whole video matched best, N when part N of a long transcript did)."""
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    query = query.strip()
    if not query:
        return []
    filters = {"category": category, "status": status, "tags": tags, "saved_after": since}
    pool = max(50, k * 5)

    vector = embed.embed_one(query) if mode != "keywords" else None  # loads the model on first use
    with db.connect() as conn:
        ranked = db.vector_ranking(conn, vector, pool=pool, **filters) if vector is not None else []
        ranked = [(v, sim, part) for v, sim, part in ranked if sim >= get_settings().search_min_similarity]
        keyword_ids = db.keyword_ranking(conn, query, pool=pool, **filters) if mode != "meaning" else []
        fused = fuse_rankings([v for v, _, _ in ranked], keyword_ids, k)
        summaries = db.videos_summary(conn, [v for v, _, _ in fused])

    similarity = {v: sim for v, sim, _ in ranked}
    part = {v: p for v, _, p in ranked}
    return [
        {**summaries[v], "score": score, "matched": matched, "similarity": similarity.get(v), "part": part.get(v, 0)}
        for v, score, matched in fused if v in summaries
    ]


def main() -> None:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query")
    parser.add_argument("--mode", choices=MODES, default="both")
    parser.add_argument("--category", choices=CATEGORY_NAMES, metavar="CATEGORY")
    parser.add_argument("--status", choices=("unprocessed", "noted"))
    parser.add_argument("--tag", action="append", dest="tags", help="match any of these tags (repeatable)")
    parser.add_argument("--since", type=datetime.fromisoformat, help="saved on or after this date (YYYY-MM-DD)")
    parser.add_argument("-k", type=int, default=10, help="number of results")
    args = parser.parse_args()

    results = search(args.query, mode=args.mode, category=args.category, status=args.status,
                     tags=args.tags, since=args.since, k=args.k)
    if not results:
        print("no matches")
    for r in results:
        saved = r["date_saved"].date().isoformat() if r["date_saved"] else "?"
        sim = f"{r['similarity']:.2f}" if r["similarity"] is not None else " -- "
        where = f" (part {r['part']})" if r.get("part") else ""
        print(f"{sim}{where}  [{r['matched']}]  {r['title'] or 'TikTok ' + r['video_id']}  [{', '.join(r['categories'] or [])}] {r['status']} {saved}")
        print(f"       {r['canonical_url']}")


if __name__ == "__main__":
    main()
