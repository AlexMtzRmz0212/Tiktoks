from datetime import datetime

import yaml

from tiktok_archive.export_obsidian import export_rows, folder_name, render_note


def make_row(**overrides) -> dict:
    row = {
        "video_id": "7688047916308499744",
        "username": "someone",
        "canonical_url": "https://www.tiktok.com/@someone/video/7688047916308499744",
        "date_saved": datetime(2026, 9, 21, 19, 10, 8),
        "status": "unprocessed",
        "title": "Three Excel tricks: XLOOKUP & more",
        "caption": "Three Excel tricks: XLOOKUP & more #excel #formulas",
        "transcript": "Today I'll show you XLOOKUP and LET.",
        "categories": ["Excel", "Life Hacks"],
        "tags": ["excel", "formulas"],
        "transcript_source": "captions",
    }
    row.update(overrides)
    return row


def frontmatter(text: str) -> dict:
    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n")[1])


def test_frontmatter_round_trips_and_keeps_types_dataview_friendly():
    fm = frontmatter(render_note(make_row()))
    assert fm["video_id"] == "7688047916308499744"  # a string, not a (lossy) number
    assert fm["category"] == "Excel"
    assert fm["categories"] == ["Excel", "Life Hacks"]
    assert fm["status"] == "unprocessed"
    assert fm["tags"] == ["excel", "formulas"]
    assert fm["date_saved"] == "2026-09-21T19:10:08"
    assert fm["aliases"] == ["Three Excel tricks: XLOOKUP & more"]


def test_body_has_caption_and_transcript():
    text = render_note(make_row())
    assert "# Three Excel tricks: XLOOKUP & more" in text
    assert "@someone · saved 2026-09-21 · [Open on TikTok](" in text
    assert "#excel #formulas\n\n## Transcript\n\nToday I'll show you XLOOKUP and LET." in text


def test_body_without_transcript_has_no_empty_section():
    text = render_note(make_row(transcript=None))
    assert "## Transcript" not in text


def test_folder_name_is_filesystem_safe():
    assert folder_name("Apps & Tools") == "Apps & Tools"
    assert folder_name('Weird: "name"?') == "Weird- -name--"


def test_export_writes_then_skips_unchanged(tmp_path):
    row = make_row()
    stats = export_rows([row], tmp_path)
    note = tmp_path / "Excel" / "7688047916308499744.md"
    assert note.exists() and stats.written == 1
    assert export_rows([row], tmp_path).unchanged == 1


def test_export_moves_note_when_primary_category_changes(tmp_path):
    export_rows([make_row()], tmp_path)
    stats = export_rows([make_row(categories=["Life Hacks"], status="noted")], tmp_path)
    assert stats.moved == 1 and stats.written == 1
    assert not (tmp_path / "Excel" / "7688047916308499744.md").exists()
    moved = tmp_path / "Life Hacks" / "7688047916308499744.md"
    assert frontmatter(moved.read_text(encoding="utf-8"))["status"] == "noted"


def test_export_leaves_other_files_alone(tmp_path):
    (tmp_path / "Excel").mkdir()
    own = tmp_path / "Excel" / "My own note.md"
    own.write_text("mine", encoding="utf-8")
    (tmp_path / ".obsidian").mkdir()
    export_rows([make_row()], tmp_path)
    assert own.read_text(encoding="utf-8") == "mine"
    assert sorted(p.name for p in (tmp_path / "Excel").iterdir()) == ["7688047916308499744.md", "My own note.md"]
