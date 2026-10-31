"""M-Team category taxonomy and search option mappings.

The numbers are M-Team's public ``/torrent/categoryList`` IDs (verified live on
2026-10-31).  The UI only ever sends leaf IDs; parent selections expand to all
of their children so the API receives a precise ``categories`` array.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class MTeamCategory:
    id: str
    name: str


@dataclass(frozen=True, slots=True)
class CategoryGroup:
    key: str
    name: str
    mode: str
    children: tuple[MTeamCategory, ...]
    adult: bool = False

    def ids(self) -> tuple[str, ...]:
        return tuple(child.id for child in self.children)


CATEGORY_GROUPS: tuple[CategoryGroup, ...] = (
    CategoryGroup(
        key="movie",
        name="电影",
        mode="movie",
        children=(
            MTeamCategory("401", "电影/SD"),
            MTeamCategory("419", "电影/HD"),
            MTeamCategory("420", "电影/DVDiSo"),
            MTeamCategory("421", "电影/BluRay"),
            MTeamCategory("439", "电影/Remux"),
        ),
    ),
    CategoryGroup(
        key="tvshow",
        name="影剧/综艺",
        mode="tvshow",
        children=(
            MTeamCategory("403", "影剧/综艺/SD"),
            MTeamCategory("402", "影剧/综艺/HD"),
            MTeamCategory("438", "影剧/综艺/BluRay"),
            MTeamCategory("435", "影剧/综艺/DVDiSo"),
        ),
    ),
    CategoryGroup(
        key="anime",
        name="动画",
        mode="anime",
        children=(
            MTeamCategory("405", "动画"),
            MTeamCategory("453", "动画/BluRay"),
        ),
    ),
    CategoryGroup(
        key="documentary",
        name="纪录片",
        mode="normal",
        children=(MTeamCategory("404", "纪录片"),),
    ),
    CategoryGroup(
        key="music",
        name="音乐",
        mode="music",
        children=(
            MTeamCategory("406", "演唱会"),
            MTeamCategory("434", "音乐（无损）"),
        ),
    ),
    CategoryGroup(
        key="game",
        name="游戏",
        mode="normal",
        children=(
            MTeamCategory("423", "PC 游戏"),
            MTeamCategory("448", "TV 游戏"),
        ),
    ),
    CategoryGroup(
        key="misc",
        name="综合",
        mode="normal",
        children=(
            MTeamCategory("407", "运动"),
            MTeamCategory("427", "电子书"),
            MTeamCategory("422", "软件"),
            MTeamCategory("442", "有声书"),
            MTeamCategory("451", "教育影片"),
            MTeamCategory("409", "其他"),
        ),
    ),
    CategoryGroup(
        key="adult",
        name="成人",
        mode="adult",
        adult=True,
        children=(
            MTeamCategory("410", "有码/HD"),
            MTeamCategory("424", "有码/SD"),
            MTeamCategory("437", "有码/DVDiSo"),
            MTeamCategory("431", "有码/BluRay"),
            MTeamCategory("429", "无码/HD"),
            MTeamCategory("430", "无码/SD"),
            MTeamCategory("426", "无码/DVDiSo"),
            MTeamCategory("432", "无码/BluRay"),
            MTeamCategory("436", "网站/0Day"),
            MTeamCategory("440", "Gay/HD"),
            MTeamCategory("425", "IV/影片"),
            MTeamCategory("433", "IV/图片"),
            MTeamCategory("411", "H-Game"),
            MTeamCategory("412", "H-Anime"),
            MTeamCategory("413", "H-Comic"),
        ),
    ),
)

GROUP_BY_KEY: dict[str, CategoryGroup] = {group.key: group for group in CATEGORY_GROUPS}
CATEGORY_INDEX: dict[str, tuple[CategoryGroup, MTeamCategory]] = {
    child.id: (group, child) for group in CATEGORY_GROUPS for child in group.children
}

MODES: tuple[str, ...] = ("normal", "movie", "tvshow", "anime", "music", "all", "adult")
MODE_LABELS: dict[str, str] = {
    "normal": "普通",
    "movie": "电影",
    "tvshow": "影剧/综艺",
    "anime": "动画",
    "music": "音乐",
    "all": "全部模式",
    "adult": "成人",
}

# 搜索分类 -> 订阅“媒体库分类”的预填值。M-Team 的“影剧/综艺”没有单一
# 对应的媒体库目录，默认按“电视剧”归档；动画沿用豆瓣的“动漫”别名。
LIBRARY_CATEGORY_BY_GROUP: dict[str, str] = {
    "movie": "电影",
    "tvshow": "电视剧",
    "anime": "动漫",
    "documentary": "纪录片",
    "music": "音乐",
    "game": "游戏",
    "misc": "综合",
    "adult": "成人",
}

# 二级分类中真正区分内容类型的值优先于父分类；电影/影剧/动画/成人下的
# SD、HD、BluRay、DVDiSo、Remux 等只是画质档，用作媒体库目录没有意义，
# 因此不在此表中，继续沿用父分类的预填值。
LIBRARY_CATEGORY_BY_SUBCATEGORY: dict[str, str] = {
    "406": "演唱会",
    "434": "音乐（无损）",
    "423": "PC 游戏",
    "448": "TV 游戏",
    "407": "运动",
    "427": "电子书",
    "422": "软件",
    "442": "有声书",
    "451": "教育影片",
    "409": "其他",
    # 成人分类按内容区分为有码/无码与专题，而不是画质档。
    "410": "有码",
    "424": "有码",
    "437": "有码",
    "431": "有码",
    "429": "无码",
    "430": "无码",
    "426": "无码",
    "432": "无码",
    "436": "网站",
    "440": "Gay",
    "425": "IV",
    "433": "IV",
    "411": "H-Game",
    "412": "H-Anime",
    "413": "H-Comic",
}

# key -> (API sortField, API sortDirection, UI label)
SORT_OPTIONS: dict[str, tuple[str, str, str]] = {
    "created_desc": ("CREATED_DATE", "DESC", "最新发布"),
    "created_asc": ("CREATED_DATE", "ASC", "最早发布"),
    "seeders_desc": ("SEEDERS", "DESC", "做种最多"),
    "leechers_desc": ("LEECHERS", "DESC", "下载中最多"),
    "completed_desc": ("TIMES_COMPLETED", "DESC", "完成最多"),
    "size_desc": ("SIZE", "DESC", "体积最大"),
    "size_asc": ("SIZE", "ASC", "体积最小"),
    "name_asc": ("NAME", "ASC", "名称 A→Z"),
}
DEFAULT_SORT = "created_desc"

DISCOUNT_OPTIONS: tuple[tuple[str, str], ...] = (
    ("", "全部"),
    ("FREE", "免费"),
    ("_2X_FREE", "2X 免费"),
    ("_2X", "2X"),
    ("_2X_PERCENT_50", "2X + 50%"),
    ("PERCENT_50", "50%"),
    ("PERCENT_70", "70%"),
    ("NORMAL", "原价"),
)
DISCOUNT_VALUES = {value for value, _ in DISCOUNT_OPTIONS if value}

PAGE_SIZES: tuple[int, ...] = (20, 50, 100)
DEFAULT_PAGE_SIZE = 50


def normalize_mode(value: str | None) -> str:
    mode = (value or "").strip()
    return mode if mode in MODES else "normal"


def normalize_sort(value: str | None) -> str:
    key = (value or "").strip()
    return key if key in SORT_OPTIONS else DEFAULT_SORT


def sort_params(value: str | None) -> tuple[str, str]:
    field, direction, _ = SORT_OPTIONS[normalize_sort(value)]
    return field, direction


def sort_label(value: str | None) -> str:
    return SORT_OPTIONS[normalize_sort(value)][2]


def normalize_discount(value: str | None) -> str:
    discount = (value or "").strip()
    return discount if discount in DISCOUNT_VALUES else ""


def normalize_page_size(value: int | str | None) -> int:
    try:
        size = int(value) if value is not None else DEFAULT_PAGE_SIZE
    except (TypeError, ValueError):
        return DEFAULT_PAGE_SIZE
    return size if size in PAGE_SIZES else DEFAULT_PAGE_SIZE


def resolve_category(
    group_key: str | None, sub_id: str | None
) -> tuple[CategoryGroup | None, list[str] | None]:
    """Resolve a parent/subcategory selection into API ``categories`` leaf IDs.

    A valid subcategory always wins.  An unknown subcategory falls back to the
    selected parent (all leaves); unknown parents are ignored.
    """
    group = GROUP_BY_KEY.get((group_key or "").strip())
    sub = (sub_id or "").strip()
    if sub:
        found = CATEGORY_INDEX.get(sub)
        if found is not None:
            sub_group, child = found
            return sub_group, [child.id]
    if group is not None:
        return group, list(group.ids())
    return None, None


def category_label(category_id: str | None) -> str:
    if not category_id:
        return ""
    found = CATEGORY_INDEX.get(str(category_id))
    return found[1].name if found is not None else str(category_id)


def library_category_for(
    group: CategoryGroup | None, sub_id: str | None = None
) -> str:
    """Pre-fill the subscription media category from a search filter.

    A selected subcategory wins when it names a distinct content type (for
    example 演唱会 or 电子书); quality-only leaves such as 电影/HD fall back to
    the parent mapping so the media library never gets a "电影/HD" folder.
    """
    found = CATEGORY_INDEX.get(str(sub_id)) if sub_id else None
    if found is not None:
        override = LIBRARY_CATEGORY_BY_SUBCATEGORY.get(found[1].id)
        if override:
            return override
    if group is None:
        return ""
    return LIBRARY_CATEGORY_BY_GROUP.get(group.key, group.name)


def mode_for_filter(group: CategoryGroup | None, requested: str | None) -> str:
    """Category groups imply their M-Team mode; otherwise honor the query."""
    return group.mode if group is not None else normalize_mode(requested)
