"""Stage 5, read side: semantic search over summaries, with category/status/tag filters.

Usage:
    python -m tiktok_archive.search "how to negotiate a raise"
    python -m tiktok_archive.search "easy dinner" --category Food --status unprocessed -k 20
    python -m tiktok_archive.search "python automation" --tag python --since 2025-01-01
"""

import argparse
from datetime import datetime

from . import db, embed
from .categories import CATEGORY_NAMES
from .config import utf8_console


def search(
    query: str,
    *,
    category: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
    since: datetime | None = None,
    k: int = 10,
) -> list[dict]:
    vector = embed.embed_one(query)
    with db.connect() as conn:
        return db.semantic_search(conn, vector, category=category, status=status, tags=tags, saved_after=since, k=k)


def main() -> None:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("query")
    parser.add_argument("--category", choices=CATEGORY_NAMES, metavar="CATEGORY")
    parser.add_argument("--status", choices=("unprocessed", "noted"))
    parser.add_argument("--tag", action="append", dest="tags", help="match any of these tags (repeatable)")
    parser.add_argument("--since", type=datetime.fromisoformat, help="saved on or after this date (YYYY-MM-DD)")
    parser.add_argument("-k", type=int, default=10, help="number of results")
    args = parser.parse_args()

    results = search(args.query, category=args.category, status=args.status, tags=args.tags, since=args.since, k=args.k)
    if not results:
        print("no matches")
    for r in results:
        saved = r["date_saved"].date().isoformat() if r["date_saved"] else "?"
        print(f"{r['similarity']:.3f}  {r['title']}  [{', '.join(r['categories'] or [])}] {r['status']} {saved}")
        print(f"       {r['canonical_url']}")


if __name__ == "__main__":
    main()
