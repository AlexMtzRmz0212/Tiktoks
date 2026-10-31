"""Stages 2-5: transcribe, classify locally, store in Postgres, embed.

`process_video` is the single per-video entry point. This module's CLI (and
run_pipeline.py) loads new rows from favorites.csv and runs it over the work
queue; a future save-time capture hook (Stage 7) can call it for one new link.

Usage:
    python -m tiktok_archive.process [--limit N] [--workers 4]
    python -m tiktok_archive.process --ids 7688047916308499744 7685551818059009300
    python -m tiktok_archive.process --reprocess --category "Job Tips"

This does not touch the vault; run_pipeline.py does both.
"""

import argparse
import logging
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from . import classify, db, embed, loader, transcribe
from .categories import CATEGORY_NAMES
from .config import get_settings, utf8_console

log = logging.getLogger(__name__)


@dataclass
class Result:
    video_id: str
    outcome: str  # 'done' | 'failed' | 'unavailable' | 'skipped'
    transcript_source: str | None = None
    categories: list[str] = field(default_factory=list)
    error: str | None = None


# --- one video ------------------------------------------------------------------

def process_video(
    video_id: str,
    *,
    reclassify: bool = False,
    keep_media: bool = False,
) -> Result:
    """Take one video as far through the pipeline as it will go.

    Stage 2 is skipped when a transcript is already stored, so a retry doesn't
    download and transcribe again. With reclassify=True a finished video goes
    through Stage 3 again from its stored transcript (e.g. after editing the
    category definitions).
    """
    workdir = get_settings().media_dir / video_id
    try:
        with db.connect() as conn:
            row = db.get_video(conn, video_id)
        if row["processing_state"] == "done" and not reclassify:
            return Result(video_id, "skipped", row["transcript_source"], row["categories"] or [])

        # Stage 2
        if row["transcript_source"] is None:
            t = transcribe.transcribe_video(row["canonical_url"], workdir)
            with db.connect() as conn:
                db.save_transcription(conn, video_id, t)
                row = db.get_video(conn, video_id)

        # Stage 3 + 5: one embedding serves both categorisation and search
        title = classify.title_from_caption(row["caption"])
        tags = classify.tags_from_hashtags(row["hashtags"])
        vector = embed.embed_one(classify.embedding_text(row["caption"], row["hashtags"], row["transcript"]))
        categories = classify.classify(vector)
        cfg = get_settings()
        chunks = classify.chunk_text(row["transcript"], size=cfg.chunk_chars, max_chunks=cfg.chunk_max)
        chunk_vectors = embed.embed(chunks) if chunks else []

        # Stage 4
        with db.connect() as conn:
            db.save_classification(conn, video_id, title=title, categories=categories, tags=tags, embedding=vector)
            db.replace_chunks(conn, video_id, chunks, chunk_vectors)
        return Result(video_id, "done", row["transcript_source"], categories)

    except transcribe.VideoUnavailable as e:
        _record_failure(video_id, str(e), unavailable=True)
        return Result(video_id, "unavailable", error=_first_line(e))
    except Exception as e:  # one bad video must not stop a multi-hour backfill
        log.debug("%s failed", video_id, exc_info=True)
        _record_failure(video_id, f"{type(e).__name__}: {e}")
        return Result(video_id, "failed", error=f"{type(e).__name__}: {_first_line(e)}")
    finally:
        if not keep_media:
            shutil.rmtree(workdir, ignore_errors=True)


def _first_line(e: BaseException) -> str:
    return (str(e).strip().splitlines() or [""])[0][:300]


def _record_failure(video_id: str, error: str, *, unavailable: bool = False) -> None:
    try:
        with db.connect() as conn:
            db.mark_failed(conn, video_id, error, unavailable=unavailable)
    except Exception:
        log.exception("could not record failure for %s", video_id)


# --- many videos ----------------------------------------------------------------

def add_processing_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--limit", type=int, help="process at most N videos this run")
    parser.add_argument("--workers", type=int, default=4, help="videos in flight at once (whisper still runs one at a time)")
    parser.add_argument("--ids", nargs="+", metavar="VIDEO_ID", help="only these videos")
    parser.add_argument("--reprocess", action="store_true",
                        help="re-run classification and embedding on already processed videos, from their stored transcripts")
    parser.add_argument("--category", choices=CATEGORY_NAMES, metavar="CATEGORY", help="with --reprocess: videos in this category")
    parser.add_argument("--keep-media", action="store_true", help="keep downloaded videos in MEDIA_DIR")


def select_video_ids(args: argparse.Namespace) -> list[str]:
    with db.connect() as conn:
        if args.reprocess:
            return db.fetch_reprocess_ids(conn, args.ids, args.category, args.limit)
        if args.ids:
            return args.ids[: args.limit] if args.limit else args.ids
        return db.fetch_queue(conn, get_settings().max_attempts, args.limit)


def run_queue(
    video_ids: list[str],
    *,
    workers: int = 4,
    reclassify: bool = False,
    keep_media: bool = False,
) -> tuple[list[Result], bool]:
    """Process videos in parallel. Returns (results, interrupted). Ctrl+C finishes in-flight videos, then stops."""
    results: list[Result] = []
    total = len(video_ids)
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = [
        pool.submit(process_video, vid, reclassify=reclassify, keep_media=keep_media)
        for vid in video_ids
    ]
    interrupted = False
    try:
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            _log_result(len(results), total, r)
    except KeyboardInterrupt:
        interrupted = True
        log.warning("interrupted: finishing the videos already in progress, then stopping (re-run to resume)")
        pool.shutdown(wait=True, cancel_futures=True)
        seen = {r.video_id for r in results}
        results += [f.result() for f in futures if f.done() and not f.cancelled() and f.result().video_id not in seen]
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return results, interrupted


def _log_result(i: int, total: int, r: Result) -> None:
    prefix = f"[{i}/{total}] {r.video_id}"
    if r.outcome == "done":
        log.info("%s done  %s  %s", prefix, r.transcript_source, ", ".join(r.categories))
    elif r.outcome == "skipped":
        log.info("%s already done", prefix)
    else:
        log.warning("%s %s  %s", prefix, r.outcome, r.error)


def summarize(results: list[Result]) -> str:
    outcomes = Counter(r.outcome for r in results)
    done = [r for r in results if r.outcome == "done"]
    sources = Counter(r.transcript_source for r in done)

    lines = [
        "",
        f"processed {len(results)}: " + ", ".join(f"{n} {k}" for k, n in outcomes.most_common()),
    ]
    if done:
        lines.append("transcripts: " + ", ".join(f"{n} {k}" for k, n in sources.most_common()))
    failures = [r for r in results if r.outcome == "failed"]
    for r in failures[:10]:
        lines.append(f"  failed {r.video_id}: {r.error}")
    if len(failures) > 10:
        lines.append(f"  ... and {len(failures) - 10} more (see videos.last_error)")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> tuple[list[Result], bool]:
    """Load new favorites (unless targeting specific videos), then process the selected videos."""
    s = get_settings()
    if not (args.ids or args.reprocess):
        inserted = loader.load_favorites()
        log.info("%d new videos loaded from %s", len(inserted), s.favorites_csv)
    video_ids = select_video_ids(args)
    if not video_ids:
        log.info("nothing to process")
        return [], False
    log.info("processing %d videos (%d workers)", len(video_ids), args.workers)
    results, interrupted = run_queue(
        video_ids, workers=args.workers, reclassify=args.reprocess, keep_media=args.keep_media,
    )
    print(summarize(results))
    return results, interrupted


def setup_logging() -> None:
    utf8_console()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "sentence_transformers", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_processing_args(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
