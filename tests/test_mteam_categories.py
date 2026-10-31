from __future__ import annotations

from app.services.mteam_categories import (
    CATEGORY_GROUPS,
    CATEGORY_INDEX,
    DEFAULT_SORT,
    DISCOUNT_OPTIONS,
    category_label,
    library_category_for,
    mode_for_filter,
    normalize_discount,
    normalize_page_size,
    normalize_sort,
    resolve_category,
    sort_params,
)


def test_category_ids_are_unique_and_all_leaf_ids_known() -> None:
    ids = [child.id for group in CATEGORY_GROUPS for child in group.children]
    assert len(ids) == len(set(ids))
    assert set(ids) == set(CATEGORY_INDEX)


def test_parent_selection_expands_to_all_children() -> None:
    group, categories = resolve_category("movie", "")
    assert group is not None and group.key == "movie"
    assert categories == ["401", "419", "420", "421", "439"]


def test_subcategory_wins_and_implies_group() -> None:
    group, categories = resolve_category("", "402")
    assert group is not None and group.key == "tvshow"
    assert categories == ["402"]


def test_unknown_subcategory_falls_back_to_parent() -> None:
    _, categories = resolve_category("anime", "999")
    assert categories == ["405", "453"]
    _, categories = resolve_category("", "999")
    assert categories is None


def test_category_label_known_and_unknown() -> None:
    assert category_label("419") == "电影/HD"
    assert category_label("999") == "999"
    assert category_label(None) == ""


def test_search_group_maps_to_media_library_category() -> None:
    def category_for(key: str) -> str:
        group, _ = resolve_category(key, "")
        return library_category_for(group)

    assert category_for("movie") == "电影"
    assert category_for("tvshow") == "电视剧"
    assert category_for("anime") == "动漫"
    assert category_for("documentary") == "纪录片"
    assert library_category_for(None) == ""


def test_semantic_subcategory_overrides_parent_library_category() -> None:
    group, _ = resolve_category("music", "")
    assert library_category_for(group, "406") == "演唱会"
    assert library_category_for(group, "434") == "音乐（无损）"

    group, _ = resolve_category("misc", "")
    assert library_category_for(group, "427") == "电子书"

    # 画质档不是内容类型，继续沿用父分类。
    group, _ = resolve_category("movie", "")
    assert library_category_for(group, "419") == "电影"
    group, _ = resolve_category("anime", "")
    assert library_category_for(group, "453") == "动漫"


def test_adult_group_implies_adult_mode() -> None:
    group, categories = resolve_category("adult", "")
    assert group is not None
    assert group.adult is True
    assert mode_for_filter(group, "normal") == "adult"
    assert len(categories or []) == 15


def test_sort_normalization_uses_official_enum_values() -> None:
    assert normalize_sort("seeders_desc") == "seeders_desc"
    assert normalize_sort("bogus") == DEFAULT_SORT
    assert sort_params("size_asc") == ("SIZE", "ASC")
    assert sort_params("bogus") == ("CREATED_DATE", "DESC")


def test_discount_and_page_size_normalization() -> None:
    assert normalize_discount("FREE") == "FREE"
    assert normalize_discount("free") == ""
    assert {value for value, _ in DISCOUNT_OPTIONS} >= {"", "FREE", "PERCENT_50"}
    assert normalize_page_size("100") == 100
    assert normalize_page_size("30") == 50
    assert normalize_page_size(None) == 50
