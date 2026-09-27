"""Stage 2: download a TikTok with yt-dlp and get its transcript.

Built-in captions are used when the video has them. Otherwise the audio goes
through local faster-whisper. Only one whisper job runs at a time, so parallel
workers can download while the GPU is busy.

Usage (smoke test, no database needed):
    python -m tiktok_archive.transcribe https://www.tiktok.com/@user/video/123 [--keep-media]
"""

import argparse
import html
import logging
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

import yt_dlp
from yt_dlp.utils import DownloadError

from .config import get_settings, utf8_console

log = logging.getLogger(__name__)

MEDIA_EXTS = {".mp4", ".webm", ".mov", ".mkv", ".m4a", ".mp3"}
AUDIO_EXTS = {".m4a", ".mp3"}  # what photo-mode (slideshow) posts download as

# yt-dlp error text for posts that are gone or private (these are never retried).
UNAVAILABLE_RE = re.compile(
    r"not available|unavailable|status code 10\d{3}|does not exist|has been removed|is private|private video",
    re.IGNORECASE,
)
HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
TIMESTAMP_LINE_RE = re.compile(r"-->")
TAG_RE = re.compile(r"<[^>]+>")


class VideoUnavailable(Exception):
    """The post was deleted, made private or is otherwise gone for good."""


@dataclass
class Transcription:
    transcript: str
    source: str  # 'captions' | 'whisper' | 'none'
    language: str | None
    caption: str | None
    hashtags: list[str] = field(default_factory=list)
    username: str | None = None
    duration_seconds: int | None = None
    video_path: Path | None = None


# --- captions -------------------------------------------------------------------

def subtitles_to_text(raw: str) -> str:
    """Flatten a WebVTT or SRT file to plain text: drop headers, cue numbers, timings, tags and repeats."""
    lines = raw.splitlines()
    out: list[str] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or TIMESTAMP_LINE_RE.search(s):
            continue
        if s.startswith(("WEBVTT", "NOTE", "STYLE", "REGION", "Kind:", "Language:")):
            continue
        # SRT cue numbers (and numeric VTT cue ids) sit directly above a timing line.
        if s.isdigit() and i + 1 < len(lines) and TIMESTAMP_LINE_RE.search(lines[i + 1]):
            continue
        s = html.unescape(TAG_RE.sub("", s)).strip()
        if s and (not out or out[-1] != s):
            out.append(s)
    return " ".join(out)


def pick_caption_track(tracks: dict[str, dict], prefs: tuple[str, ...]) -> tuple[str, dict] | None:
    """Choose a subtitle track by language-code prefix preference, falling back to the first one."""
    available = {lang: t for lang, t in tracks.items() if t and t.get("filepath") and lang != "live_chat"}
    if not available:
        return None
    for pref in prefs:
        for lang in sorted(available):
            if lang.lower().startswith(pref):
                return lang, available[lang]
    lang = sorted(available)[0]
    return lang, available[lang]


def _caption_text(info: dict) -> tuple[str, str | None]:
    track = pick_caption_track(info.get("requested_subtitles") or {}, get_settings().caption_langs)
    if not track:
        return "", None
    lang, entry = track
    path = Path(entry["filepath"])
    if not path.exists():
        return "", None
    return subtitles_to_text(path.read_text(encoding="utf-8", errors="replace")), lang


# --- download -------------------------------------------------------------------

def _ydl_opts(workdir: Path, *, subtitles: bool) -> dict:
    s = get_settings()
    opts = {
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        # A single file with both audio (for whisper) and video (for keyframes).
        "format": "best[height<=720]/best",
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "retries": 3,
        "socket_timeout": 30,
    }
    if subtitles:
        opts.update(
            writesubtitles=True,
            writeautomaticsub=True,
            subtitleslangs=["all"],
            subtitlesformat="vtt/srt/best",
        )
    if s.ytdlp_cookies_from_browser:
        opts["cookiesfrombrowser"] = (s.ytdlp_cookies_from_browser,)
    return opts


def _find_media(workdir: Path) -> Path | None:
    files = [p for p in workdir.glob("*") if p.suffix.lower() in MEDIA_EXTS]
    return max(files, key=lambda p: p.stat().st_size) if files else None


def download(url: str, workdir: Path, *, subtitles: bool = True) -> tuple[dict, Path]:
    """Download the video (and optionally every subtitle track) into workdir."""
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(workdir, subtitles=subtitles)) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadError as e:
        msg = str(e)
        if subtitles and "subtitle" in msg.lower():
            # A broken subtitle track shouldn't cost us the video; whisper can cover it.
            log.warning("subtitle download failed, retrying without subtitles: %s", msg)
            return download(url, workdir, subtitles=False)
        if UNAVAILABLE_RE.search(msg):
            raise VideoUnavailable(msg) from e
        raise
    downloads = info.get("requested_downloads") or []
    path = Path(downloads[0]["filepath"]) if downloads and downloads[0].get("filepath") else _find_media(workdir)
    if path is None or not path.exists():
        raise RuntimeError(f"yt-dlp reported success but no media file is in {workdir}")
    return info, path


def ensure_video(url: str, workdir: Path) -> Path:
    """The downloaded video, fetching it again if it was already cleaned up."""
    existing = _find_media(workdir) if workdir.exists() else None
    return existing or download(url, workdir, subtitles=False)[1]


def slideshow_images(video_id: str, out_dir: Path, max_images: int = 6, width: int = 768) -> list[Path]:
    """Slides of a photo-mode post, as JPEGs. yt-dlp only downloads such posts' music track.

    This goes through a private yt-dlp method, so any failure just means no slides.
    """
    try:
        with yt_dlp.YoutubeDL(_ydl_opts(out_dir, subtitles=False)) as ydl:
            ie = ydl.get_info_extractor("TikTok")
            data, _status = ie._extract_web_data_and_status(ie._create_url(None, video_id), video_id)
            images = ((data or {}).get("imagePost") or {}).get("images") or []
            out_dir.mkdir(parents=True, exist_ok=True)
            slides = []
            for i, image in enumerate(images[:max_images]):
                urls = (image.get("imageURL") or {}).get("urlList") or []
                if not urls:
                    continue
                raw = out_dir / f"slide_{i}.src"
                raw.write_bytes(ydl.urlopen(urls[0]).read())
                jpg = out_dir / f"slide_{i}.jpg"
                # Slides often carry the content as text, so they stay larger than video keyframes.
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-vf", f"scale='min({width},iw)':-2", "-q:v", "3", str(jpg)],
                    check=False, capture_output=True, timeout=60,
                )
                if jpg.exists() and jpg.stat().st_size > 0:
                    slides.append(jpg)
            return slides
    except Exception as e:
        log.warning("%s: could not fetch slideshow images: %s", video_id, e)
        return []


# --- whisper --------------------------------------------------------------------

_whisper_model = None
_whisper_device: str | None = None
_whisper_lock = threading.Lock()  # one GPU job at a time; also guards the lazy load


def _load_whisper(force_cpu: bool = False):
    from faster_whisper import WhisperModel

    s = get_settings()
    candidates = []
    if s.whisper_device in ("auto", "cuda") and not force_cpu:
        candidates.append(("cuda", s.whisper_compute_type or "int8_float16"))
    if s.whisper_device in ("auto", "cpu") or force_cpu:
        candidates.append(("cpu", "int8" if s.whisper_device != "cpu" else (s.whisper_compute_type or "int8")))
    last_error: Exception | None = None
    for device, compute_type in candidates:
        try:
            model = WhisperModel(s.whisper_model, device=device, compute_type=compute_type)
            log.info("loaded whisper %s on %s (%s)", s.whisper_model, device, compute_type)
            return model, device
        except Exception as e:  # missing CUDA/cuDNN libraries surface as assorted exception types
            log.warning("could not load whisper on %s: %s", device, e)
            last_error = e
    raise RuntimeError(f"could not load whisper model {s.whisper_model}") from last_error


def whisper_transcribe(path: Path) -> tuple[str, str | None]:
    """Transcribe speech in a media file. Returns (text, language); text is '' when nobody speaks."""
    global _whisper_model, _whisper_device
    with _whisper_lock:
        if _whisper_model is None:
            _whisper_model, _whisper_device = _load_whisper()
        try:
            text, language = _run_whisper(path)
        except RuntimeError as e:
            # CUDA libraries that load fine can still be missing pieces at inference time.
            if _whisper_device != "cuda" or get_settings().whisper_device == "cuda":
                raise
            log.warning("whisper failed on GPU (%s); falling back to CPU", e)
            _whisper_model, _whisper_device = _load_whisper(force_cpu=True)
            text, language = _run_whisper(path)
    return text, language


def _run_whisper(path: Path) -> tuple[str, str | None]:
    # vad_filter skips non-speech, which keeps whisper from hallucinating words over music.
    segments, info = _whisper_model.transcribe(str(path), vad_filter=True, beam_size=5)
    text = " ".join(seg.text.strip() for seg in segments if seg.text.strip())
    return text, (info.language if text else None)


# --- Stage 2 entry point -------------------------------------------------------

def transcribe_video(url: str, workdir: Path) -> Transcription:
    info, video_path = download(url, workdir)

    description = (info.get("description") or "").strip() or None
    hashtags = list(dict.fromkeys(
        [t.lstrip("#") for t in (info.get("tags") or []) if t]
        + HASHTAG_RE.findall(description or "")
    ))
    duration = info.get("duration")

    text, language = _caption_text(info)
    if text:
        source = "captions"
    else:
        text, language = whisper_transcribe(video_path)
        source = "whisper" if text else "none"

    return Transcription(
        transcript=text,
        source=source,
        language=language,
        caption=description,
        hashtags=hashtags,
        username=info.get("uploader") or info.get("channel"),
        duration_seconds=round(duration) if duration else None,
        video_path=video_path,
    )


def main() -> None:
    utf8_console()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("url")
    parser.add_argument("--keep-media", action="store_true", help="keep the downloaded files in MEDIA_DIR/_cli")
    args = parser.parse_args()

    workdir = get_settings().media_dir / "_cli"
    try:
        t = transcribe_video(args.url, workdir)
        print(f"source:    {t.source}")
        print(f"language:  {t.language}")
        print(f"username:  {t.username}")
        print(f"duration:  {t.duration_seconds}s")
        print(f"hashtags:  {', '.join(t.hashtags)}")
        print(f"caption:   {t.caption}")
        print(f"transcript ({len(t.transcript.split())} words):\n{t.transcript}")
    except VideoUnavailable as e:
        print(f"unavailable: {e}")
    finally:
        if not args.keep_media:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
