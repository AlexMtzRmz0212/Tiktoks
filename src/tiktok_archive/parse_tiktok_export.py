#!/usr/bin/env python3
"""
Parse a TikTok "Download your data" JSON export, pull out the Favorite
Videos list, resolve each share link to its canonical tiktok.com URL, and
write a deduplicated CSV ready to load into a database.

Usage:
    pip install requests
    python parse_tiktok_export.py --input /path/to/unzipped_export --output favorites.csv

--input can be the unzipped export folder (the script searches it
recursively for .json files) or a single .json file directly.

Safe to interrupt and re-run: already-resolved links are skipped on the
next run by reading back what's already in --output.
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

VIDEO_URL_RE = re.compile(r"tiktok\.com/@([\w.\-]+)/video/(\d+)")

# Where FavoriteVideoList lives differs between export versions.
FAVORITES_PARENTS = ("Activity", "Likes and Favorites")


def find_favorite_entries(root: Path):
    json_files = [root] if root.is_file() else list(root.rglob("*.json"))
    entries = []
    for jf in json_files:
        try:
            data = json.loads(jf.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        for parent in FAVORITES_PARENTS:
            fav = (
                (data.get(parent) or {})
                .get("Favorite Videos", {})
                .get("FavoriteVideoList")
            )
            if fav:
                entries.extend(fav)
                break
    return entries


SHARE_ID_RE = re.compile(r"^https?://[\w.\-]+/share/video/(\d+)")


def id_from_link(link: str) -> str | None:
    """The video id, when the export's share link already contains it (no network needed)."""
    m = SHARE_ID_RE.match(link or "")
    return m.group(1) if m else None


def resolve_link(link: str, session: requests.Session, retries: int = 3):
    """Return (canonical_url, video_id, username). username is None when it isn't known yet.

    The export's usual links (tiktokv.com/share/video/<id>/) carry the id, so no request is
    made: TikTok answers plain web requests with "403 Access Denied", and yt-dlp, which
    Stage 2 uses, accepts an address without a username (it learns the username from the post).
    Other link shapes (short links) are followed over the network.
    """
    video_id = id_from_link(link)
    if video_id:
        return f"https://www.tiktok.com/@/video/{video_id}", video_id, None
    for attempt in range(retries):
        try:
            resp = session.get(link, allow_redirects=True, timeout=10)
            match = VIDEO_URL_RE.search(resp.url)
            if match:
                username, video_id = match.group(1), match.group(2)
                canonical = f"https://www.tiktok.com/@{username}/video/{video_id}"
                return canonical, video_id, username
            return None, None, None
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
    return None, None, None


def load_progress(output_path: Path):
    seen_links = set()
    seen_video_ids = set()
    if output_path.exists():
        with output_path.open(newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("share_link"):
                    seen_links.add(row["share_link"])
                if row.get("video_id"):
                    seen_video_ids.add(row["video_id"])
    return seen_links, seen_video_ids


def main():
    parser = argparse.ArgumentParser(description="Parse TikTok Favorites export into a clean CSV")
    parser.add_argument("--input", required=True, help="Unzipped export folder, or a single .json file")
    parser.add_argument("--output", default="favorites.csv", help="Output CSV path")
    parser.add_argument("--failures", default="favorites_failed.csv", help="CSV for links that failed to resolve")
    parser.add_argument("--workers", type=int, default=10, help="Concurrent resolution requests")
    args = parser.parse_args()

    root = Path(args.input)
    output_path = Path(args.output)
    failures_path = Path(args.failures)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    failures_path.parent.mkdir(parents=True, exist_ok=True)

    entries = find_favorite_entries(root)
    if not entries:
        sys.exit("No Favorite Videos found under that path — check --input points at the export.")

    print(f"Found {len(entries)} favorited videos in the export.")

    seen_links, seen_video_ids = load_progress(output_path)
    to_process = [e for e in entries if e.get("Link") not in seen_links]
    print(f"{len(entries) - len(to_process)} already resolved on a previous run, {len(to_process)} to go.")

    write_header = not output_path.exists()
    out_f = output_path.open("a", newline="", encoding="utf-8")
    fail_f = failures_path.open("a", newline="", encoding="utf-8")
    out_writer = csv.writer(out_f)
    fail_writer = csv.writer(fail_f)
    if write_header:
        out_writer.writerow(["video_id", "username", "canonical_url", "share_link", "date_saved"])

    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(resolve_link, e["Link"], session): e for e in to_process}
        for future in as_completed(futures):
            entry = futures[future]
            canonical, video_id, username = future.result()
            done += 1
            if done % 100 == 0:
                print(f"...{done}/{len(to_process)}")

            if not canonical or video_id in seen_video_ids:
                if not canonical:
                    fail_writer.writerow([entry.get("Link"), entry.get("Date")])
                continue

            seen_video_ids.add(video_id)
            out_writer.writerow([video_id, username, canonical, entry.get("Link"), entry.get("Date")])

    out_f.close()
    fail_f.close()
    print(f"Done. Wrote results to {output_path}, failures (if any) to {failures_path}.")


if __name__ == "__main__":
    main()
