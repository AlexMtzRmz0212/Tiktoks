import pytest

from tiktok_archive import search
from tiktok_archive.ui import server


def test_fuse_puts_videos_found_by_both_lists_first():
    fused = search.fuse_rankings(["a", "b", "c"], ["c", "d"], limit=10)
    assert [v for v, _, _ in fused][0] == "c"  # 3rd by meaning + 1st by words beats 1st by meaning alone
    matched = {v: m for v, _, m in fused}
    assert matched == {"a": "meaning", "b": "meaning", "c": "both", "d": "keywords"}


def test_fuse_keeps_each_list_order_and_respects_the_limit():
    fused = search.fuse_rankings(["a", "b", "c"], [], limit=2)
    assert [v for v, _, _ in fused] == ["a", "b"]
    assert search.fuse_rankings([], [], limit=5) == []


def test_fuse_ties_are_broken_by_id_so_results_are_stable():
    fused = search.fuse_rankings(["b"], ["a"], limit=5)  # same score
    assert [v for v, _, _ in fused] == ["a", "b"]


def test_search_rejects_a_bad_mode_and_ignores_empty_queries():
    with pytest.raises(ValueError):
        search.search("x", mode="telepathy")
    assert search.search("   ") == []


def test_search_api_validates_input():
    for query in ({}, {"q": ["x"], "mode": ["nope"]}, {"q": ["x"], "category": ["Nope"]}, {"q": ["x"], "status": ["nope"]}, {"q": ["x" * 400]}):
        with pytest.raises(ValueError):
            server.search_videos(query)
