"""Stage 3: turn a transcript (plus caption, hashtags and sometimes keyframes) into notes with Claude.

The output is structured (title, summary, key points, categories, tags), and
categories are restricted to the closed set by the JSON schema itself.

Keyframes are only sent when on-screen content matters. The category isn't
known until Claude answers, so frames go on the first call when the video looks
visual (little speech, or visual keywords in the caption/hashtags), and
otherwise a second call with frames runs only if Claude picks 3D, Piano or
Gadgets.
"""

import base64
import logging
import re
import subprocess
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import anthropic
from pydantic import BaseModel

from .categories import CATEGORIES, CATEGORY_NAMES, VISUAL_CATEGORIES, VISUAL_KEYWORDS
from .config import get_settings

log = logging.getLogger(__name__)

Category = Literal[CATEGORY_NAMES]  # type: ignore[valid-type]

MAX_TOKENS = 8000
SHORT_TRANSCRIPT_WORDS = 25
KEYFRAME_POSITIONS = (0.10, 0.35, 0.60, 0.85)
KEYFRAME_WIDTH = 512

# USD per million tokens (input, output), for the run summary's cost estimate only.
PRICES = {
    "claude-sonnet-5": (2.00, 10.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-opus-5-5": (4.00, 20.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


class VideoNotes(BaseModel):
    title: str
    summary: str
    key_points: list[str]
    categories: list[Category]
    tags: list[str]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.calls += other.calls

    def cost_usd(self, model: str) -> float | None:
        if model not in PRICES:
            return None
        price_in, price_out = PRICES[model]
        return (
            self.input_tokens * price_in
            + self.cache_write_tokens * price_in * 1.25
            + self.cache_read_tokens * price_in * 0.1
            + self.output_tokens * price_out
        ) / 1_000_000


@dataclass
class Analysis:
    notes: VideoNotes
    used_keyframes: bool
    usage: Usage = field(default_factory=Usage)


class AnalysisError(Exception):
    pass


# --- prompt ---------------------------------------------------------------------

def system_prompt(language: str) -> str:
    categories = "\n".join(f"- {name}: {desc}" for name, desc in CATEGORIES.items())
    return f"""You turn saved TikTok videos into short reference notes for a personal knowledge archive. The person saved each video because something in it seemed worth keeping; your notes should let them recall and reuse that without rewatching.

You get the video's transcript (from built-in captions or speech-to-text, so expect errors, and it may be in any language), the uploader's caption and hashtags, and sometimes images: a few stills from the video, or the slides of a photo post, where the text on the slides is usually the actual content.

Write:
- title: a plain descriptive title of at most 10 words. No clickbait, no emoji.
- summary: 2-4 sentences on what the video actually says or shows.
- key_points: 3-5 concrete takeaways worth remembering: specific steps, names, tools, numbers, recommendations. Skip filler like "the creator explains".
- categories: 1-3 categories from the list below, most relevant first. Add a second or third only when the video genuinely spans them.
- tags: 3-8 short topic tags in lowercase kebab-case (for example "salary-negotiation", "python", "sourdough"). When an existing tag fits, reuse it exactly rather than inventing a near-synonym.

Write the title, summary, key points and tags in {language}, whatever language the video is in.

If the transcript is empty, is just song lyrics, or looks like speech-to-text noise, work from the caption, hashtags and frames. If the content still can't be determined, say so plainly in the summary instead of guessing.

Categories:
{categories}"""


def _metadata_block(row: dict, existing_tags: list[str]) -> str:
    parts = [
        f"Uploader: @{row.get('username') or 'unknown'}",
        f"Duration: {row['duration_seconds']}s" if row.get("duration_seconds") else None,
        f"Caption: {row.get('caption') or '(none)'}",
        f"Hashtags: {', '.join('#' + h for h in row.get('hashtags') or []) or '(none)'}",
        f"Transcript source: {row.get('transcript_source') or 'unknown'}",
        f"Transcript:\n{row.get('transcript') or '(no speech detected)'}",
    ]
    if existing_tags:
        parts.append(f"Existing tags you can reuse: {', '.join(existing_tags)}")
    return "\n\n".join(p for p in parts if p)


# --- keyframes ------------------------------------------------------------------

def extract_keyframes(video_path: Path, duration: float | None, out_dir: Path) -> list[Path]:
    """Grab up to 4 evenly spread frames with ffmpeg, scaled to 512px wide JPEGs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    times = [duration * f for f in KEYFRAME_POSITIONS] if duration else [0.5, 2.0, 4.0, 6.0]
    frames = []
    for i, t in enumerate(times):
        out = out_dir / f"frame_{i}.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y", "-loglevel", "error",
                    "-ss", f"{t:.2f}", "-i", str(video_path),
                    "-frames:v", "1", "-vf", f"scale={KEYFRAME_WIDTH}:-2", "-q:v", "4",
                    str(out),
                ],
                check=False,
                capture_output=True,
                timeout=60,
            )
        except FileNotFoundError:
            log.warning("ffmpeg is not on PATH; continuing without keyframes")
            return []
        if out.exists() and out.stat().st_size > 0:
            frames.append(out)
    return frames


def looks_visual(row: dict) -> bool:
    """Guess, before calling Claude, whether frames will matter for this video."""
    if len((row.get("transcript") or "").split()) < SHORT_TRANSCRIPT_WORDS:
        return True
    text = " ".join([row.get("caption") or "", *(row.get("hashtags") or [])]).lower()
    words = set(re.findall(r"[a-z0-9]+", text))
    return any(k in words for k in VISUAL_KEYWORDS)


# --- post-processing ------------------------------------------------------------

def normalize_tag(tag: str) -> str:
    """Lowercase ASCII kebab-case, which Obsidian accepts as a tag. Purely numeric tags aren't valid there."""
    t = unicodedata.normalize("NFKD", tag).encode("ascii", "ignore").decode()
    t = re.sub(r"[^a-z0-9]+", "-", t.lower().lstrip("#")).strip("-")
    return "" if t.isdigit() else t


def clean_notes(notes: VideoNotes) -> VideoNotes:
    tags = [t for t in dict.fromkeys(normalize_tag(t) for t in notes.tags) if t]
    return VideoNotes(
        title=notes.title.strip(),
        summary=notes.summary.strip(),
        key_points=[k.strip() for k in notes.key_points if k.strip()][:5],
        categories=list(dict.fromkeys(notes.categories))[:3],
        tags=tags[:8],
    )


# --- Claude ---------------------------------------------------------------------

_client: anthropic.Anthropic | None = None


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        # Reads ANTHROPIC_API_KEY from the environment (.env is loaded by config). Extra
        # retries because a long backfill will occasionally hit 429s and 5xxs.
        _client = anthropic.Anthropic(max_retries=5)
    return _client


def _call_claude(row: dict, frames: list[Path], existing_tags: list[str], model: str) -> tuple[VideoNotes, Usage]:
    s = get_settings()
    content: list[dict] = [
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": base64.standard_b64encode(f.read_bytes()).decode()},
        }
        for f in frames
    ]
    if frames:
        content.append({
            "type": "text",
            "text": f"The {len(frames)} images above are from the post, in order: stills from the video, or the slides of a photo post.",
        })
    content.append({"type": "text", "text": _metadata_block(row, existing_tags)})

    response = get_client().messages.parse(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[{"type": "text", "text": system_prompt(s.notes_language), "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": content}],
        thinking={"type": "adaptive"},
        output_config={"effort": s.anthropic_effort},
        output_format=VideoNotes,
    )

    u = response.usage
    usage = Usage(
        input_tokens=u.input_tokens,
        output_tokens=u.output_tokens,
        cache_read_tokens=u.cache_read_input_tokens or 0,
        cache_write_tokens=u.cache_creation_input_tokens or 0,
        calls=1,
    )
    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        raise AnalysisError(f"Claude declined this video ({getattr(details, 'category', None) or 'no category'})")
    if response.stop_reason == "max_tokens":
        raise AnalysisError(f"response hit max_tokens={MAX_TOKENS}")
    if response.parsed_output is None:
        raise AnalysisError(f"no structured output (stop_reason={response.stop_reason})")
    return clean_notes(response.parsed_output), usage


def analyze(
    row: dict,
    frames_provider: Callable[[], list[Path]],
    existing_tags: list[str],
    model: str | None = None,
) -> Analysis:
    """Run Stage 3 for one video row (needs transcript, caption, hashtags, username, duration)."""
    model = model or get_settings().anthropic_model
    usage = Usage()

    frames = frames_provider() if looks_visual(row) else []
    notes, u = _call_claude(row, frames, existing_tags, model)
    usage.add(u)

    if not frames and VISUAL_CATEGORIES.intersection(notes.categories):
        frames = frames_provider()
        if frames:
            log.info("%s: %s video, re-running with keyframes", row["video_id"], "/".join(notes.categories))
            notes, u = _call_claude(row, frames, existing_tags, model)
            usage.add(u)

    return Analysis(notes=notes, used_keyframes=bool(frames), usage=usage)
