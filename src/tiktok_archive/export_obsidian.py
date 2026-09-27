"""Stage 6: write one Obsidian note per processed video, Postgres -> vault (one way).

Notes go to <VAULT_PATH>/<primary category>/<video_id>.md. Re-running updates
notes in place: a note whose primary category changed is moved, and a file is
only rewritten when its content would change. Only files named <video_id>.md
for videos in the database are ever touched. Nothing in the vault is read back
into Postgres, so any edits made in Obsidian are overwritten on the next export.

Usage:
    python -m tiktok_archive.export_obsidian                 # every processed video
    python -m tiktok_archive.export_obsidian --ids 7688047916308499744
"""

import argparse
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from . import db
from .config import get_settings, utf8_console

log = logging.getLogger(__name__)

NOTE_STEM_RE = re.compile(r"^\d{6,25}$")
INVALID_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SKIP_DIRS = {".obsidian", ".trash", ".git"}


@dataclass
class ExportStats:
    written: int = 0
    moved: int = 0
    unchanged: int = 0

    def __str__(self) -> str:
        return f"{self.written} written, {self.moved} moved, {self.unchanged} unchanged"


def folder_name(category: str) -> str:
    """A category name that is safe as a folder name on Windows, macOS and Linux."""
    return INVALID_PATH_CHARS_RE.sub("-", category).strip(" .") or "Uncategorized"


def note_path(vault: Path, row: dict) -> Path:
    primary = (row.get("categories") or ["Uncategorized"])[0]
    return vault / folder_name(primary) / f"{row['video_id']}.md"


def render_note(row: dict) -> str:
    title = (row.get("title") or "").strip() or f"TikTok {row['video_id']}"
    categories = list(row.get("categories") or [])
    saved = row.get("date_saved")
    frontmatter = {
        "video_id": str(row["video_id"]),
        "title": title,
        "aliases": [title],
        "category": categories[0] if categories else None,
        "categories": categories,
        "status": row.get("status"),
        "tags": list(row.get("tags") or []),
        "date_saved": saved.isoformat(timespec="seconds") if saved else None,
        "url": row["canonical_url"],
        "username": row.get("username"),
        "transcript_source": row.get("transcript_source"),
    }
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=10_000)

    byline = " · ".join(
        part for part in (
            f"@{row['username']}" if row.get("username") else None,
            f"saved {saved.date().isoformat()}" if saved else None,
            f"[Open on TikTok]({row['canonical_url']})",
        ) if part
    )
    key_points = "\n".join(f"- {k}" for k in row.get("key_points") or [])
    body = [
        f"# {title}",
        byline,
        "## Summary",
        (row.get("summary") or "").strip(),
        "## Key points",
        key_points,
    ]
    return f"---\n{fm}---\n\n" + "\n\n".join(part for part in body if part) + "\n"


def index_existing_notes(vault: Path) -> dict[str, list[Path]]:
    """Map video_id -> note files already in the vault (normally exactly one)."""
    found: dict[str, list[Path]] = {}
    if not vault.exists():
        return found
    for path in vault.rglob("*.md"):
        if SKIP_DIRS.intersection(path.relative_to(vault).parts):
            continue
        if NOTE_STEM_RE.match(path.stem):
            found.setdefault(path.stem, []).append(path)
    return found


def export_rows(rows: Iterable[dict], vault: Path) -> ExportStats:
    stats = ExportStats()
    existing = index_existing_notes(vault)
    for row in rows:
        target = note_path(vault, row)
        content = render_note(row)
        current = existing.get(row["video_id"], [])

        if target not in current and current:
            # The primary category changed since the last export: move the note.
            source = current[0]
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                log.warning("%s exists in two places; leaving %s", row["video_id"], source)
            else:
                source.rename(target)
                stats.moved += 1
            if len(current) > 1:
                log.warning("%s has several notes: %s", row["video_id"], ", ".join(map(str, current)))

        if target.exists() and target.read_text(encoding="utf-8") == content:
            stats.unchanged += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
        stats.written += 1
    return stats


def export(ids: Sequence[str] | None = None, vault: Path | None = None) -> ExportStats:
    """Export processed videos (all of them, or just `ids`) to the vault."""
    vault = vault or get_settings().vault_path
    if ids is not None and not ids:
        return ExportStats()
    with db.connect() as conn:
        rows = db.fetch_for_export(conn, ids)
    return export_rows(rows, vault)


def main() -> None:
    utf8_console()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ids", nargs="+", metavar="VIDEO_ID", help="only these videos (default: all processed)")
    parser.add_argument("--vault", type=Path, help="defaults to VAULT_PATH")
    args = parser.parse_args()
    vault = args.vault or get_settings().vault_path
    stats = export(args.ids, vault)
    print(f"{vault}: {stats}")


if __name__ == "__main__":
    main()
