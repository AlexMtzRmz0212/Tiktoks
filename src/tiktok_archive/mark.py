"""Set your review status on videos, then re-export their notes.

status is yours alone: the pipeline never changes it. Since the vault is a
read-only view, this is how a video becomes 'noted'.

Usage:
    python -m tiktok_archive.mark 7688047916308499744 [7685551818059009300 ...]
    python -m tiktok_archive.mark --unnote 7688047916308499744
"""

import argparse

from . import db
from .config import utf8_console
from .export_obsidian import export


def main() -> None:
    utf8_console()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("video_ids", nargs="+")
    parser.add_argument("--unnote", action="store_true", help="set back to 'unprocessed'")
    args = parser.parse_args()

    status = "unprocessed" if args.unnote else "noted"
    with db.connect() as conn:
        updated = db.set_status(conn, args.video_ids, status)
    missing = sorted(set(args.video_ids) - set(updated))
    print(f"{len(updated)} set to {status}" + (f"; not in the database: {', '.join(missing)}" if missing else ""))
    if updated:
        print(f"vault: {export(updated)}")


if __name__ == "__main__":
    main()
