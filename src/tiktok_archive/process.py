"""Stages 2-5: transcribe, understand with Claude, store in Postgres, embed.

`process_video` is the single per-video entry point. This module's CLI (and
run_pipeline.py) loads new rows from favorites.csv and runs it over the work
queue; a future save-time capture hook (Stage 7) can call it for one new link.

Usage:
    python -m tiktok_archive.process [--limit N] [--workers 4]
    python -m tiktok_archive.process --ids 7688047916308499744 7685551818059009300
    python -m tiktok_archive.process --reprocess --category "Job Tips" --model claude-opus-5

This does not touch the vault; run_pipeline.py does both.
"""

import argparse
import logging
import shutil
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from . import db, embed, loader, transcribe, understand
from .categories import CATEGORY_NAMES
from .config import get_settings, utf8_console

log = logging.getLogger(__name__)


@dataclass
class Result:
    video_id: str
    outcome: str  # 'done' | 'failed' | 'unavailable' | 'skipped'
    transcript_source: str | None = None
    categories: list[str] = field(default_factory=list)
    used_keyframes: bool = False
    usage: understand.Usage = field(default_factory=understand.Usage)
    error: str | None = None


# --- one video ------------------------------------------------------------------

def process_video(
    video_id: str,
    *,
    model: str | None = None,
    reanalyze: bool = False,
    keep_media: bool = False,
) -> Result:
    """Take one video as far through the pipeline as it will go.

    Stage 2 is skipped when a transcript is already stored, so a retry after a
    Claude failure doesn't download and transcribe again. With reanalyze=True a
    finished video goes through Stage 3 again (e.g. after switching models).
    """
    model = model or get_settings().anthropic_model
    workdir = get_settings().media_dir / video_id
    try:
        with db.connect() as conn:
            row = db.get_video(conn, video_id)
        if row["processing_state"] == "done" and not reanalyze:
            return Result(video_id, "skipped", row["transcript_source"], row["categories"] or [])

        # Stage 2
        if row["transcript_source"] is None:
            t = transcribe.transcribe_video(row["canonical_url"], workdir)
            with db.connect() as conn:
                db.save_transcription(conn, video_id, t)
                row = db.get_video(conn, video_id)

        # Stage 3
        def frames() -> list[Path]:
            media = transcribe.ensure_video(row["canonical_url"], workdir)
            if media.suffix.lower() in transcribe.AUDIO_EXTS:
                # Photo-mode post: yt-dlp only gets its music, so show Claude the slides instead.
                return transcribe.slideshow_images(video_id, workdir / "slides")
            return understand.extract_keyframes(media, row["duration_seconds"], workdir / "frames")

        with db.connect() as conn:
            existing_tags = db.top_tags(conn)
        analysis = understand.analyze(row, frames, existing_tags, model=model)
        notes = analysis.notes

        # Stage 5, computed first so that Stage 4 is a single UPDATE
        vector = embed.embed_one(embed.notes_text(notes.title, notes.summary, notes.key_points))

        # Stage 4
        with db.connect() as conn:
            db.save_analysis(
                conn, video_id, notes,
                model=model, used_keyframes=analysis.used_keyframes, embedding=vector,
            )
        return Result(
            video_id, "done", row["transcript_source"], list(notes.categories),
            analysis.used_keyframes, analysis.usage,
        )

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
    parser.add_argument("--model", help="Claude model for this run (default: ANTHROPIC_MODEL)")
    parser.add_argument("--reprocess", action="store_true",
                        help="re-run Stage 3 on already processed videos; needs --ids, --category, --analyzed-with or --limit")
    parser.add_argument("--category", choices=CATEGORY_NAMES, metavar="CATEGORY", help="with --reprocess: videos in this category")
    parser.add_argument("--analyzed-with", metavar="MODEL", help="with --reprocess: videos analysed by this model")
    parser.add_argument("--keep-media", action="store_true", help="keep downloaded videos in MEDIA_DIR")


def select_video_ids(args: argparse.Namespace) -> list[str]:
    with db.connect() as conn:
        if args.reprocess:
            if not (args.ids or args.category or args.analyzed_with or args.limit):
                raise SystemExit("--reprocess re-runs paid Claude calls; narrow it with --ids, --category, --analyzed-with or --limit")
            return db.fetch_reprocess_ids(conn, args.ids, args.category, args.analyzed_with, args.limit)
        if args.ids:
            return args.ids[: args.limit] if args.limit else args.ids
        return db.fetch_queue(conn, get_settings().max_attempts, args.limit)


def run_queue(
    video_ids: list[str],
    *,
    workers: int = 4,
    model: str | None = None,
    reanalyze: bool = False,
    keep_media: bool = False,
) -> tuple[list[Result], bool]:
    """Process videos in parallel. Returns (results, interrupted). Ctrl+C finishes in-flight videos, then stops."""
    results: list[Result] = []
    total = len(video_ids)
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = [
        pool.submit(process_video, vid, model=model, reanalyze=reanalyze, keep_media=keep_media)
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
        frames = " +frames" if r.used_keyframes else ""
        log.info("%s done  %s%s  %s", prefix, r.transcript_source, frames, ", ".join(r.categories))
    elif r.outcome == "skipped":
        log.info("%s already done", prefix)
    else:
        log.warning("%s %s  %s", prefix, r.outcome, r.error)


def summarize(results: list[Result], model: str) -> str:
    outcomes = Counter(r.outcome for r in results)
    done = [r for r in results if r.outcome == "done"]
    sources = Counter(r.transcript_source for r in done)
    usage = understand.Usage()
    for r in results:
        usage.add(r.usage)
    cost = usage.cost_usd(model)

    lines = [
        "",
        f"processed {len(results)}: " + ", ".join(f"{n} {k}" for k, n in outcomes.most_common()),
    ]
    if done:
        lines.append("transcripts: " + ", ".join(f"{n} {k}" for k, n in sources.most_common())
                     + f"; {sum(r.used_keyframes for r in done)} used keyframes")
    if usage.calls:
        lines.append(
            f"Claude ({model}): {usage.calls} calls, {usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens:,} input"
            f" + {usage.output_tokens:,} output tokens"
            + (f", about ${cost:.2f}" if cost is not None else "")
        )
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
    model = args.model or s.anthropic_model
    log.info("processing %d videos with %s (%d workers)", len(video_ids), model, args.workers)
    results, interrupted = run_queue(
        video_ids, workers=args.workers, model=model, reanalyze=args.reprocess, keep_media=args.keep_media,
    )
    print(summarize(results, model))
    return results, interrupted


def setup_logging() -> None:
    utf8_console()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S")
    for noisy in ("httpx", "httpx2", "anthropic", "sentence_transformers", "faster_whisper"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def main() -> None:
    setup_logging()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_processing_args(parser)
    run(parser.parse_args())


if __name__ == "__main__":
    main()
