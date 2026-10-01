import numpy as np

from tiktok_archive import classify
from tiktok_archive.categories import CATEGORY_NAMES


def scores_with(**by_name: float) -> np.ndarray:
    scores = np.full(len(CATEGORY_NAMES), 0.1)
    for name, value in by_name.items():
        scores[CATEGORY_NAMES.index(name.replace("_", " "))] = value
    return scores


def test_title_is_first_caption_line_without_hashtags():
    assert classify.title_from_caption("Three Excel tricks #excel #fyp\nmore text") == "Three Excel tricks"
    assert classify.title_from_caption("#fyp\n\nSecond line") == "Second line"


def test_title_is_none_without_text():
    assert classify.title_from_caption(None) is None
    assert classify.title_from_caption("#fyp #viral") is None


def test_long_title_is_cut_at_a_word_boundary():
    title = classify.title_from_caption("word " * 40)
    assert title.endswith("…") and len(title) <= classify.TITLE_MAX_CHARS + 1
    assert not title.rstrip("…").endswith("wor")


def test_normalize_tag():
    assert classify.normalize_tag("#Python Tips") == "python-tips"
    assert classify.normalize_tag("café") == "cafe"
    assert classify.normalize_tag("2026") == ""
    assert classify.normalize_tag("🔥") == ""


def test_tags_are_deduped_and_capped():
    assert classify.tags_from_hashtags(["Excel", "excel", "FYP", "2026"]) == ["excel", "fyp"]
    assert len(classify.tags_from_hashtags([f"tag{i}" for i in range(20)])) == classify.MAX_TAGS
    assert classify.tags_from_hashtags(None) == []


def test_embedding_text_joins_parts_and_truncates_transcript():
    text = classify.embedding_text("A caption", ["excel", "tips"], "x" * 5000)
    assert text.startswith("A caption\n#excel #tips\n")
    assert len(text) == len("A caption\n#excel #tips\n") + classify.TRANSCRIPT_CHARS
    assert classify.embedding_text(None, None, "only transcript") == "only transcript"


def test_best_category_is_always_picked_even_with_low_scores():
    assert classify.pick_categories(scores_with(Excel=0.2)) == ["Excel"]


def test_close_runner_ups_are_added_best_first():
    picked = classify.pick_categories(scores_with(Excel=0.60, Life_Hacks=0.59, Coding=0.58))
    assert picked == ["Excel", "Life Hacks", "Coding"]


def test_distant_or_weak_runner_ups_are_left_out():
    assert classify.pick_categories(scores_with(Excel=0.60, Food=0.40)) == ["Excel"]  # too far behind
    assert classify.pick_categories(scores_with(Excel=0.25, Food=0.24)) == ["Excel"]  # below the floor


def test_never_more_than_three_categories():
    scores = scores_with(Excel=0.6, Food=0.6, Coding=0.6, Music=0.6, Piano=0.6)
    assert len(classify.pick_categories(scores)) == classify.MAX_CATEGORIES
