from __future__ import annotations

import pytest

from app.clients.release_name import (
    cn_to_int,
    has_chinese_audio,
    normalize_title,
    parse_release,
    season_from_title,
    title_similarity,
    title_variants,
)


@pytest.mark.parametrize(
    "name,season,episodes,resolution,codec",
    [
        ("[Group] 示例剧名 S01E05 1080p WEB-DL H.265 中字", 1, (5,), "1080p", "h265"),
        ("示例剧名.2023.S02E10.2160p.WEB-DL.x265-GROUP", 2, (10,), "2160p", "h265"),
        ("Show.S01E01E02.1080p", 1, (1, 2), "1080p", None),
        ("Show.S01E01-03.1080p", 1, (1, 2, 3), "1080p", None),
        ("Show S01E01 1080p WEB-DL", 1, (1,), "1080p", None),
        ("动漫 第2季 第05集 1080p", 2, (5,), "1080p", None),
        ("Show.EP07.720p", None, (7,), "720p", None),
        ("Show.S02.Complete.1080p", 2, (), "1080p", None),
        ("Movie.2023.1080p.BluRay.x264", None, (), "1080p", "h264"),
    ],
)
def test_parse_release(name, season, episodes, resolution, codec):
    parsed = parse_release(name)
    assert parsed.season == season
    assert parsed.episodes == episodes
    assert parsed.resolution == resolution
    assert parsed.video_codec == codec


def test_resolution_not_parsed_as_episode():
    parsed = parse_release("Show.S01E01.1080p")
    assert parsed.episodes == (1,)


def test_batch_total():
    parsed = parse_release("剧名 全12集 1080p")
    assert parsed.is_batch is True
    assert parsed.episodes == tuple(range(1, 13))


@pytest.mark.parametrize(
    "name",
    [
        "嫁金钗.S01.更新至14集.2160p.WEB-DL-G",
        "嫁金钗 更新至第14集 1080p",
    ],
)
def test_update_to_is_cumulative_pack(name):
    parsed = parse_release(name)
    assert parsed.is_batch is True
    assert parsed.episodes == tuple(range(1, 15))


def test_year_and_chinese_sub():
    parsed = parse_release("[Group] 示例剧名 2024 1080p 中字")
    assert parsed.year == 2024
    assert parsed.has_chinese_sub is True
    assert parsed.group == "Group"


def test_hevc_not_chinese_sub():
    parsed = parse_release("Movie.2023.1080p.HEVC-GROUP")
    assert parsed.has_chinese_sub is False


def test_subtitle_group_not_counted_as_chinese_sub():
    parsed = parse_release("[某字幕组] Movie.2023.1080p")
    assert parsed.has_chinese_sub is False


def test_cn_numerals():
    assert cn_to_int("一") == 1
    assert cn_to_int("十") == 10
    assert cn_to_int("十二") == 12
    assert cn_to_int("二十") == 20
    assert cn_to_int("二十三") == 23


def test_normalize_title_is_stable():
    assert normalize_title("示例 剧名：第一季") == normalize_title("示例剧名第一季")
    assert normalize_title("繁體劇名") == normalize_title("繁体剧名")


def test_similarity_prefers_substring():
    assert title_similarity("示例剧名", [], "[Group] 示例剧名 S01E01 1080p") == 1.0
    assert title_similarity("完全无关的片名", [], "Show.S01E01.1080p") < 0.5


def test_similarity_uses_alias():
    score = title_similarity("中文名", ["English Name"], "English.Name.S01E01.1080p")
    assert score >= 0.6


@pytest.mark.parametrize(
    "title,season",
    [
        ("老友记 第二季", 2),
        ("Friends Season 2", 2),
        ("Friends.S02.1080p", 2),
        ("第十三季", 13),
        ("老友记", None),
        ("", None),
    ],
)
def test_season_from_title(title, season):
    assert season_from_title(title) == season


def test_title_variants_strips_season_suffix():
    assert title_variants("老友记 第二季") == ["老友记 第二季", "老友记"]
    assert title_variants("Friends Season 2") == ["Friends Season 2", "Friends"]
    assert title_variants("老友记") == ["老友记"]


def test_base_title_matches_season_pack():
    assert (
        title_similarity("老友记", ["Friends"], "Friends.S02.2160p.WEB-DL-G")
        == 1.0
    )


def test_embedded_title_is_not_a_match():
    # "老友记" is part of a different Chinese title; normalisation removed the
    # separator, so a plain substring check would wrongly accept it.
    assert title_similarity("老友记", [], "古宅老友记.S02.2160p.WEB-DL-G") == 0.0


@pytest.mark.parametrize(
    "name,depth",
    [
        ("Movie.2024.2160p.10bit.HDR-G", 10),
        ("Movie.2024.1080p.10-bit-G", 10),
        ("Show.Hi10P.1080p-G", 10),
        ("Show.1080p.8bit-G", 8),
        ("Show.1080p.WEB-DL-G", None),
    ],
)
def test_bit_depth(name, depth):
    assert parse_release(name).bit_depth == depth


@pytest.mark.parametrize(
    "texts,expected",
    [
        (("Movie.2024.国语.1080p-G",), True),
        (("Movie.2024.国配.1080p-G",), True),
        (("Movie.2024.1080p-G", "国语 / 英语 | 1080p"), True),
        (("Movie.2024.普通话.1080p-G",), True),
        (("Movie.2024.Mandarin.1080p-G",), True),
        (("Movie.2024.中字.1080p-G",), False),
        (("Movie.2024.1080p-G",), False),
    ],
)
def test_chinese_audio_detection(texts, expected):
    assert has_chinese_audio(*texts) is expected
