import re

from tiktok_archive.categories import CATEGORY_NAMES
from tiktok_archive.config import MIGRATIONS_DIR


def _sql_category_list() -> list[str]:
    sql = (MIGRATIONS_DIR / "001_init.sql").read_text(encoding="utf-8")
    array = re.search(r"categories <@ ARRAY\[(.*?)\]::TEXT\[\]", sql, re.S).group(1)
    return re.findall(r"'([^']+)'", array)


def test_db_check_constraint_matches_categories_py():
    assert _sql_category_list() == list(CATEGORY_NAMES)


def test_closed_set_is_the_agreed_32():
    assert len(CATEGORY_NAMES) == 32 == len(set(CATEGORY_NAMES))
