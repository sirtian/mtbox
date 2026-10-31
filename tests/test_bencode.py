from __future__ import annotations

import hashlib

import pytest

from app.clients.bencode import BencodeError, bdecode, parse_torrent


def bencode(value):
    if isinstance(value, bool):
        return bencode(int(value))
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        return b"d" + b"".join(bencode(k) + bencode(v) for k, v in value.items()) + b"e"
    raise TypeError(type(value))


def wrap(info: dict) -> bytes:
    return b"d4:info" + bencode(info) + b"e"


def test_single_file_torrent():
    info = {"name": "Movie.2023.1080p.mkv", "length": 1234, "piece length": 16384}
    data = wrap(info)
    meta = parse_torrent(data)
    assert meta.name == "Movie.2023.1080p.mkv"
    assert meta.total_size == 1234
    assert meta.files[0].path == "Movie.2023.1080p.mkv"
    assert meta.is_multi is False
    assert meta.info_hash == hashlib.sha1(bencode(info)).hexdigest()


def test_multi_file_torrent_with_unicode():
    info = {
        "name": "示例剧",
        "files": [
            {"length": 10, "path": ["示例剧.S01E01.mkv"]},
            {"length": 20, "path": ["字幕", "示例剧.S01E01.ass"]},
        ],
        "private": 1,
    }
    meta = parse_torrent(wrap(info))
    assert meta.total_size == 30
    assert meta.private is True
    assert [item.path for item in meta.files] == [
        "示例剧.S01E01.mkv",
        "字幕/示例剧.S01E01.ass",
    ]
    assert meta.is_multi is True
    assert meta.info_hash == hashlib.sha1(bencode(info)).hexdigest()


def test_multi_file_torrent_with_one_entry_is_still_multi():
    info = {"name": "Movie.2023", "files": [{"length": 10, "path": ["movie.mkv"]}]}
    meta = parse_torrent(wrap(info))
    assert meta.is_multi is True


def test_reject_path_traversal():
    info = {"name": "x", "files": [{"length": 1, "path": ["..", "evil.mkv"]}]}
    with pytest.raises(BencodeError):
        parse_torrent(wrap(info))


def test_reject_truncated():
    with pytest.raises(BencodeError):
        parse_torrent(b"d4:infod4:name3:ab")


def test_reject_unknown_top_level_type():
    with pytest.raises(BencodeError):
        parse_torrent(b"l4:infoe")


def test_bdecode_basic_types():
    assert bdecode(b"i42e") == 42
    assert bdecode(b"3:abc") == b"abc"
    assert bdecode(b"l1:ai1ee") == [b"a", 1]
    assert bdecode(b"d1:ai1ee") == {b"a": 1}
