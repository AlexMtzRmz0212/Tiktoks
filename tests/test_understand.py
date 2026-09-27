import pytest
from pydantic import ValidationError

from tiktok_archive.understand import (
    Usage,
    VideoNotes,
    _metadata_block,
    clean_notes,
    looks_visual,
    normalize_tag,
    system_prompt,
)


def test_normalize_tag():
    assert normalize_tag("#Salary Negotiation") == "salary-negotiation"
    assert normalize_tag("Café  Culture!") == "cafe-culture"
    assert normalize_tag("2024") == ""  # Obsidian rejects purely numeric tags
    assert normalize_tag("python3") == "python3"


def test_clean_notes_trims_and_dedupes():
    notes = VideoNotes(
        title=" Title ",
        summary=" Sum. ",
        key_points=["a", " ", "b", "c", "d", "e", "f"],
        categories=["Coding", "AI", "Coding", "Technology", "Projects"],
        tags=["Python", "python", "AI Tools", "123"],
    )
    cleaned = clean_notes(notes)
    assert cleaned.title == "Title"
    assert cleaned.key_points == ["a", "b", "c", "d", "e"]
    assert cleaned.categories == ["Coding", "AI", "Technology"]
    assert cleaned.tags == ["python", "ai-tools"]


def test_categories_are_restricted_to_closed_set():
    with pytest.raises(ValidationError):
        VideoNotes(title="t", summary="s", key_points=[], categories=["Cooking"], tags=[])
    schema = VideoNotes.model_json_schema()
    enum = schema["properties"]["categories"]["items"]["enum"]
    assert "Apps & Tools" in enum and len(enum) == 32


def test_looks_visual():
    speechy = " ".join(["word"] * 60)
    assert looks_visual({"transcript": "", "caption": "", "hashtags": []})
    assert not looks_visual({"transcript": speechy, "caption": "salary tips", "hashtags": ["career"]})
    assert looks_visual({"transcript": speechy, "caption": "my new Blender render", "hashtags": []})
    assert looks_visual({"transcript": speechy, "caption": "", "hashtags": ["piano"]})


def test_prompt_contains_every_category_and_language():
    prompt = system_prompt("English")
    assert "- Break into Tech:" in prompt and "- Excel:" in prompt
    assert "in English" in prompt


def test_metadata_block():
    block = _metadata_block(
        {"username": "u", "duration_seconds": 42, "caption": "c", "hashtags": ["a", "b"],
         "transcript_source": "whisper", "transcript": "hello"},
        ["python"],
    )
    assert "Hashtags: #a, #b" in block and "Existing tags you can reuse: python" in block


def test_usage_cost():
    u = Usage(input_tokens=1_000_000, output_tokens=100_000, calls=1)
    assert u.cost_usd("claude-sonnet-5") == pytest.approx(3.0)
    assert u.cost_usd("unknown-model") is None
