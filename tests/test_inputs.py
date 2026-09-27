import json
from datetime import datetime

import pytest

from tiktok_archive.loader import read_favorites
from tiktok_archive.parse_tiktok_export import find_favorite_entries

ENTRY = {"Date": "2026-09-21 19:10:08", "Link": "https://www.tiktokv.com/share/video/7688047916308499744/"}


@pytest.mark.parametrize("parent", ["Activity", "Likes and Favorites"])
def test_export_parser_finds_favorites_in_both_layouts(tmp_path, parent):
    f = tmp_path / "user_data_tiktok.json"
    f.write_text(json.dumps({parent: {"Favorite Videos": {"FavoriteVideoList": [ENTRY]}}}), encoding="utf-8")
    assert find_favorite_entries(f) == [ENTRY]
    assert find_favorite_entries(tmp_path) == [ENTRY]


def test_export_parser_skips_unrelated_json(tmp_path):
    (tmp_path / "list.json").write_text("[1, 2]", encoding="utf-8")
    (tmp_path / "other.json").write_text('{"Profile": {}}', encoding="utf-8")
    assert find_favorite_entries(tmp_path) == []


def test_read_favorites_dedupes_and_parses_dates(tmp_path):
    csv_path = tmp_path / "favorites.csv"
    csv_path.write_text(
        "video_id,username,canonical_url,share_link,date_saved\n"
        "1,a,https://www.tiktok.com/@a/video/1,https://www.tiktokv.com/share/video/1/,2026-09-21 19:10:08\n"
        "1,a,https://www.tiktok.com/@a/video/1,dup,2026-09-22 00:00:00\n"
        "2,,https://www.tiktok.com/@b/video/2,,\n",
        encoding="utf-8",
    )
    rows = read_favorites(csv_path)
    assert [r["video_id"] for r in rows] == ["1", "2"]
    assert rows[0]["date_saved"] == datetime(2026, 9, 21, 19, 10, 8)
    assert rows[1]["username"] is None and rows[1]["date_saved"] is None


def test_read_favorites_rejects_wrong_columns(tmp_path):
    csv_path = tmp_path / "favorites.csv"
    csv_path.write_text("Link,Date\nx,y\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        read_favorites(csv_path)
