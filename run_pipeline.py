#!/usr/bin/env python3
"""Run the whole pipeline: favorites.csv -> Postgres -> Obsidian vault.

1. Apply pending database migrations (additive only).
2. Load rows from favorites.csv that aren't in Postgres yet.
3. Process the work queue (new videos, plus earlier failures that still have retries left):
   transcribe -> Claude notes -> store -> embed.
4. Export notes for the videos processed in this run to the vault.

Safe to stop with Ctrl+C and re-run: finished videos are never redone.

Usage:
    python run_pipeline.py --limit 20            # try a small sample first
    python run_pipeline.py                        # everything that's left
    python run_pipeline.py --only-export          # rebuild/refresh all notes, no processing
    python run_pipeline.py --reprocess --category Coding --model claude-opus-5
"""

import argparse
import logging

from tiktok_archive import export_obsidian, migrate, process
from tiktok_archive.config import get_settings

log = logging.getLogger("run_pipeline")


def main() -> None:
    process.setup_logging()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    process.add_processing_args(parser)
    parser.add_argument("--only-export", action="store_true", help="skip processing; export every processed video")
    parser.add_argument("--no-export", action="store_true", help="process only; leave the vault alone")
    args = parser.parse_args()

    for version in migrate.apply_migrations(verbose=False):
        log.info("applied migration %s", version)

    vault = get_settings().vault_path
    if args.only_export:
        log.info("vault %s: %s", vault, export_obsidian.export(vault=vault))
        return

    results, interrupted = process.run(args)

    if not args.no_export:
        changed = [r.video_id for r in results if r.outcome == "done"]
        log.info("vault %s: %s", vault, export_obsidian.export(changed, vault))
    if interrupted:
        raise SystemExit(130)


if __name__ == "__main__":
    main()
