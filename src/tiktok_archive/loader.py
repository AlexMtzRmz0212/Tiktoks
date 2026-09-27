"""Load favorites.csv (Stage 1 output) into the videos table.

favorites.csv is the fixed input boundary: this only reads it. Rows already in
Postgres are left untouched, so re-running is safe.

Usage:
    python -m tiktok_archive.loader [--csv data/favorites.csv]
"""

import argparse
import csv
from datetime import datetime
from pathlib import Path

from . import db
from .config import get_settings, utf8_console

REQUIRED_COLUMNS = {"video_id", "canonical_url"}


def _parse_date(value: str | None) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def read_favorites(path: Path) -> list[dict]:
    """Parse favorites.csv, deduplicating by video_id (first occurrence wins)."""
    if not path.exists():
        raise SystemExit(f"{path} not found. Run Stage 1 (parse_tiktok_export) first, or set FAVORITES_CSV.")
    rows: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise SystemExit(f"{path} is missing columns: {', '.join(sorted(missing))}")
        for r in reader:
            video_id = (r.get("video_id") or "").strip()
            url = (r.get("canonical_url") or "").strip()
            if not video_id or not url or video_id in rows:
                continue
            rows[video_id] = {
                "video_id": video_id,
                "username": (r.get("username") or "").strip() or None,
                "canonical_url": url,
                "share_link": (r.get("share_link") or "").strip() or None,
                "date_saved": _parse_date(r.get("date_saved")),
            }
    return list(rows.values())


def load_favorites(path: Path | None = None) -> list[str]:
    """Insert new rows from favorites.csv. Returns the newly inserted video_ids."""
    rows = read_favorites(path or get_settings().favorites_csv)
    with db.connect() as conn:
        return db.insert_new_videos(conn, rows)


def main() -> None:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--csv", type=Path, help="defaults to FAVORITES_CSV")
    args = parser.parse_args()
    inserted = load_favorites(args.csv)
    print(f"{len(inserted)} new videos added to the database")


if __name__ == "__main__":
    main()
