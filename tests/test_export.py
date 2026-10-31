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


def test_dry_run_counts_without_touching_the_vault(tmp_path):
    row = make_row()
    assert export_rows([row], tmp_path, dry_run=True).written == 1
    assert list(tmp_path.iterdir()) == []

    export_rows([row], tmp_path)
    assert export_rows([row], tmp_path, dry_run=True).unchanged == 1

    moved = make_row(categories=["Life Hacks"])
    stats = export_rows([moved], tmp_path, dry_run=True)
    assert stats.moved == 1 and stats.written == 1
    assert (tmp_path / "Excel" / "7688047916308499744.md").exists()  # still where it was
    assert not (tmp_path / "Life Hacks").exists()


def test_related_section_links_similar_videos_before_the_transcript():
    related = [
        {"video_id": "7685551818059009300", "title": "Remote work tips", "similarity": 0.7, "shared_tags": ["remote", "jobs"]},
        {"video_id": "7675461313853705493", "title": None, "similarity": 0.6, "shared_tags": []},
    ]
    text = render_note(make_row(related=related))
    assert "## Related\n\n- [[7685551818059009300|Remote work tips]] · shared: #remote, #jobs\n- [[7675461313853705493|TikTok 7675461313853705493]]" in text
    assert text.index("## Related") < text.index("## Transcript")


def test_no_related_section_when_nothing_is_similar():
    assert "## Related" not in render_note(make_row())
    assert "## Related" not in render_note(make_row(related=[]))


def test_related_titles_cannot_break_the_link():
    from tiktok_archive.export_obsidian import related_line

    line = related_line({"video_id": "123456", "title": "a | b ]] c", "shared_tags": []})
    assert line == "- [[123456|a - b ] ] c]]"


def hub_topic(name="excel · xlookup", ids=("7688047916308499744", "7685551818059009300")):
    return {"topic_id": 1, "name": name, "members": [
        {"video_id": i, "title": f"video {i[-3:]}", "username": "someone"} for i in ids]}


def test_note_shows_its_topic_in_properties_and_as_a_link():
    text = render_note(make_row(topic="excel · xlookup"))
    assert frontmatter(text)["topic"] == "excel · xlookup"
    assert "Topic: [[_Topics/excel · xlookup|excel · xlookup]]" in text
    assert "topic" not in frontmatter(render_note(make_row()))  # no topic, no property


def test_topic_hub_lists_members_and_is_flagged_as_generated(tmp_path):
    from tiktok_archive.export_obsidian import export_topic_hubs

    stats = export_topic_hubs([hub_topic()], tmp_path)
    hub = tmp_path / "_Topics" / "excel · xlookup.md"
    assert stats.hubs == 1 and hub.exists()
    text = hub.read_text(encoding="utf-8")
    assert frontmatter(text) == {"tiktok_archive_topic": True, "topic": "excel · xlookup", "videos": 2}
    assert "- [[7688047916308499744|video 744]] · @someone" in text
    assert export_topic_hubs([hub_topic()], tmp_path).hubs == 0  # unchanged: not rewritten


def test_stale_hubs_are_removed_but_other_files_in_the_folder_are_not(tmp_path):
    from tiktok_archive.export_obsidian import export_topic_hubs

    export_topic_hubs([hub_topic("old topic")], tmp_path)
    mine = tmp_path / "_Topics" / "my own note.md"
    mine.write_text("mine", encoding="utf-8")
    stats = export_topic_hubs([hub_topic("new topic")], tmp_path)
    assert stats.hubs == 1 and stats.removed == 1
    assert not (tmp_path / "_Topics" / "old topic.md").exists()
    assert (tmp_path / "_Topics" / "new topic.md").exists() and mine.read_text(encoding="utf-8") == "mine"


def test_dry_run_hubs_change_nothing(tmp_path):
    from tiktok_archive.export_obsidian import export_topic_hubs

    assert export_topic_hubs([hub_topic()], tmp_path, dry_run=True).hubs == 1
    assert not (tmp_path / "_Topics").exists()
