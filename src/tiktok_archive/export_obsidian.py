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

TOPICS_DIR = "_Topics"  # hub notes live here; the export owns only files in it that carry the flag below
HUB_FLAG = "tiktok_archive_topic"
NOTE_STEM_RE = re.compile(r"^\d{6,25}$")
INVALID_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SKIP_DIRS = {".obsidian", ".trash", ".git"}


@dataclass
class ExportStats:
    written: int = 0
    moved: int = 0
    unchanged: int = 0
    hubs: int = 0  # topic hub notes written
    removed: int = 0  # topic hub notes deleted because their topic no longer exists

    def __str__(self) -> str:
        text = f"{self.written} written, {self.moved} moved, {self.unchanged} unchanged"
        if self.hubs or self.removed:
            text += f"; topic hubs: {self.hubs} written, {self.removed} removed"
        return text


def folder_name(category: str) -> str:
    """A category name that is safe as a folder name on Windows, macOS and Linux."""
    return INVALID_PATH_CHARS_RE.sub("-", category).strip(" .") or "Uncategorized"


def note_path(vault: Path, row: dict) -> Path:
    primary = (row.get("categories") or ["Uncategorized"])[0]
    return vault / folder_name(primary) / f"{row['video_id']}.md"


def topic_link(name: str) -> str:
    return f"[[{TOPICS_DIR}/{folder_name(name)}|{name}]]"


def related_line(r: dict) -> str:
    """One Obsidian link to a similar video, with the tags the two share."""
    title = ((r.get("title") or "").strip() or f"TikTok {r['video_id']}").replace("|", "-").replace("]]", "] ]")
    shared = ", ".join(f"#{t}" for t in r.get("shared_tags") or [])
    return f"- [[{r['video_id']}|{title}]]" + (f" · shared: {shared}" if shared else "")


def related_section(related: list[dict]) -> str:
    return ("## Related\n\n" + "\n".join(related_line(r) for r in related)) if related else ""


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
        **({"topic": row["topic"]} if row.get("topic") else {}),
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
    transcript = (row.get("transcript") or "").strip()
    related = row.get("related") or []
    body = [
        f"# {title}",
        byline,
        (row.get("caption") or "").strip(),
        f"Topic: {topic_link(row['topic'])}" if row.get("topic") else "",
        related_section(related),
        "## Transcript" if transcript else "",
        transcript,
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


def export_rows(rows: Iterable[dict], vault: Path, *, dry_run: bool = False) -> ExportStats:
    """Write (or, with dry_run, only count) the notes for `rows`."""
    stats = ExportStats()
    existing = index_existing_notes(vault)
    for row in rows:
        target = note_path(vault, row)
        content = render_note(row)
        current = existing.get(row["video_id"], [])
        on_disk = target  # where this note's current text lives, once any move has happened

        if target not in current and current:
            # The primary category changed since the last export: move the note.
            source = current[0]
            if target.exists():
                log.warning("%s exists in two places; leaving %s", row["video_id"], source)
            else:
                if dry_run:
                    on_disk = source
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source.rename(target)
                stats.moved += 1
            if len(current) > 1:
                log.warning("%s has several notes: %s", row["video_id"], ", ".join(map(str, current)))

        if on_disk.exists() and on_disk.read_text(encoding="utf-8") == content:
            stats.unchanged += 1
            continue
        if not dry_run:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        stats.written += 1
    return stats


def render_topic_hub(topic: dict) -> str:
    members = topic["members"]
    frontmatter = {HUB_FLAG: True, "topic": topic["name"], "videos": len(members)}
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True, width=10_000)
    lines = [
        f"- [[{m['video_id']}|{((m.get('title') or '').strip() or 'TikTok ' + m['video_id']).replace('|', '-').replace(']]', '] ]')}]]"
        + (f" · @{m['username']}" if m.get("username") else "")
        for m in members
    ]
    intro = f"{len(members)} videos on this theme, the most typical first. Generated; changes here are overwritten."
    return f"---\n{fm}---\n\n# {topic['name']}\n\n{intro}\n\n" + "\n".join(lines) + "\n"


def _is_generated_hub(path: Path) -> bool:
    try:
        return f"{HUB_FLAG}: true" in path.read_text(encoding="utf-8")[:300]
    except OSError:
        return False


def export_topic_hubs(topics: Iterable[dict], vault: Path, *, dry_run: bool = False) -> ExportStats:
    """Write one hub note per topic into <vault>/_Topics and delete hubs of topics that are gone."""
    stats = ExportStats()
    folder = vault / TOPICS_DIR
    wanted: dict[str, str] = {}
    for topic in topics:
        if topic["members"]:
            wanted[f"{folder_name(topic['name'])}.md"] = render_topic_hub(topic)
    existing = {p.name: p for p in folder.glob("*.md") if _is_generated_hub(p)} if folder.exists() else {}
    for name, content in wanted.items():
        target = folder / name
        if target.exists() and target.read_text(encoding="utf-8") == content:
            continue
        if not dry_run:
            folder.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="\n")
        stats.hubs += 1
    for name, path in existing.items():
        if name not in wanted:
            if not dry_run:
                path.unlink()
            stats.removed += 1
    return stats


def rows_for_export(conn, ids: Sequence[str] | None = None) -> list[dict]:
    """Finished videos, each with the similar videos its note will link to and its topic's name."""
    s = get_settings()
    rows = db.fetch_for_export(conn, ids)
    names = {t["topic_id"]: t["name"] for t in db.topics_with_members(conn)}
    for r in rows:
        r["topic"] = names.get(r.get("topic_id"))
    related = db.related_videos(
        conn, [r["video_id"] for r in rows], k=s.related_max, min_similarity=s.related_min_similarity,
    ) if s.related_max > 0 else {}
    for r in rows:
        r["related"] = related.get(r["video_id"], [])
    return rows


def export(ids: Sequence[str] | None = None, vault: Path | None = None) -> ExportStats:
    """Export processed videos (all of them, or just `ids`) to the vault."""
    vault = vault or get_settings().vault_path
    if ids is not None and not ids:
        return ExportStats()
    with db.connect() as conn:
        rows = rows_for_export(conn, ids)
        topics = db.topics_with_members(conn)
    stats = export_rows(rows, vault)
    hubs = export_topic_hubs(topics, vault)
    stats.hubs, stats.removed = hubs.hubs, hubs.removed
    return stats


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
