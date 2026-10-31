from collections import Counter

import numpy as np

from tiktok_archive import topics


def unit(rows):
    v = np.array(rows, dtype=np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def two_groups_and_outliers():
    rng = np.random.default_rng(0)
    a = np.eye(8)[0] + rng.normal(0, 0.05, (5, 8))
    b = np.eye(8)[1] + rng.normal(0, 0.05, (5, 8))
    outliers = np.eye(8)[[2, 3]]
    return unit(np.vstack([a, b, outliers]))


def test_cluster_finds_the_two_groups_and_leaves_outliers_out():
    groups = topics.cluster(two_groups_and_outliers(), similarity=0.5, min_size=4)
    assert [sorted(g) for g in groups] == [[0, 1, 2, 3, 4], [5, 6, 7, 8, 9]]


def test_cluster_needs_enough_videos_and_respects_min_size():
    assert topics.cluster(unit([[1, 0], [0, 1]]), similarity=0.5, min_size=4) == []
    assert topics.cluster(unit([[1, 0]]), similarity=0.5, min_size=1) == []
    assert topics.cluster(two_groups_and_outliers(), similarity=0.5, min_size=6) == []


def test_a_stricter_similarity_splits_groups_further():
    vectors = unit([[1, 0, 0], [0.9, 0.44, 0], [0.9, -0.44, 0], [0, 0, 1]])
    assert len(topics.cluster(vectors, similarity=0.4, min_size=3)) == 1
    assert topics.cluster(vectors, similarity=0.95, min_size=3) == []


def test_order_by_centre_puts_the_typical_video_first():
    vectors = unit([[1, 0], [0.99, 0.1], [0.8, 0.6]])
    assert topics.order_by_centre(vectors, [0, 1, 2])[0] == 1


def test_name_uses_tags_that_set_the_group_apart_and_skips_generic_ones():
    group = [["excel", "xlookup", "fyp"], ["excel", "xlookup", "fyp"], ["excel", "fyp"]]
    frequency = Counter({"excel": 3, "xlookup": 2, "fyp": 100})
    name = topics.name_topic(group, frequency, 100, [["Excel"]] * 3)
    assert name == "excel · xlookup"


def test_name_ignores_tags_used_by_only_one_video_and_falls_back_to_the_category():
    name = topics.name_topic([["a"], ["b"], ["c"]], Counter({"a": 1, "b": 1, "c": 1}), 50, [["Piano"], ["Piano"], ["Music"]])
    assert name == "Piano (mixed)"


def test_build_topics_names_are_unique_and_members_are_ordered():
    vectors = two_groups_and_outliers()
    rows = []
    for i, v in enumerate(vectors):
        rows.append({
            "video_id": str(1000 + i), "title": f"video {i}", "embedding": v,
            "tags": ["same", "tag"] if i < 10 else [], "categories": ["Excel"],
        })
    found, ungrouped = topics.build_topics(rows, similarity=0.5, min_size=4)
    assert ungrouped == 2
    assert [len(t["members"]) for t in found] == [5, 5]
    names = [t["name"] for t in found]
    assert len(set(n.lower() for n in names)) == 2  # the second one gets " (2)"
    assert set(found[0]["members"]) == {str(1000 + i) for i in range(5)}
