"""Minimal bencode decoder that preserves the raw ``info`` span for hashing."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Iterator

MAX_TORRENT_BYTES = 32 * 1024 * 1024
MAX_FILES = 20_000
MAX_DEPTH = 100


class BencodeError(ValueError):
    pass


def _parse(data: bytes, index: int = 0, depth: int = 0) -> tuple[Any, int]:
    if depth > MAX_DEPTH:
        raise BencodeError("嵌套层级过深")
    if index >= len(data):
        raise BencodeError("数据不完整")
    char = data[index : index + 1]

    if char == b"i":
        end = data.find(b"e", index)
        if end < 0:
            raise BencodeError("整数缺少结尾")
        raw = data[index + 1 : end]
        if not raw or raw == b"-" or raw.startswith(b"0") and raw != b"0":
            raise BencodeError(f"非法整数: {raw!r}")
        try:
            return int(raw), end + 1
        except ValueError as exc:
            raise BencodeError(f"非法整数: {raw!r}") from exc

    if char == b"l":
        items: list[Any] = []
        index += 1
        while True:
            if index >= len(data):
                raise BencodeError("列表缺少结尾")
            if data[index : index + 1] == b"e":
                return items, index + 1
            value, index = _parse(data, index, depth + 1)
            items.append(value)

    if char == b"d":
        result: dict[bytes, Any] = {}
        index += 1
        while True:
            if index >= len(data):
                raise BencodeError("字典缺少结尾")
            if data[index : index + 1] == b"e":
                return result, index + 1
            key, index = _parse(data, index, depth + 1)
            if not isinstance(key, bytes):
                raise BencodeError("字典键必须是字符串")
            value, index = _parse(data, index, depth + 1)
            result[key] = value

    if char.isdigit():
        colon = data.find(b":", index)
        if colon < 0:
            raise BencodeError("字符串缺少长度分隔符")
        length_raw = data[index:colon]
        if not length_raw.isdigit():
            raise BencodeError(f"非法字符串长度: {length_raw!r}")
        length = int(length_raw)
        start = colon + 1
        end = start + length
        if end > len(data):
            raise BencodeError("字符串越界")
        return data[start:end], end

    raise BencodeError(f"未知类型标记: {char!r}")


def bdecode(data: bytes) -> Any:
    value, end = _parse(data, 0)
    if end != len(data):
        raise BencodeError("尾部存在多余数据")
    return value


def _info_span(data: bytes) -> tuple[int, int]:
    if data[:1] != b"d":
        raise BencodeError("种子顶层必须是字典")
    index = 1
    while True:
        if index >= len(data):
            raise BencodeError("字典缺少结尾")
        if data[index : index + 1] == b"e":
            break
        key, index = _parse(data, index)
        if not isinstance(key, bytes):
            raise BencodeError("字典键必须是字符串")
        value_start = index
        _, index = _parse(data, index)
        if key == b"info":
            return value_start, index
    raise BencodeError("种子缺少 info 字段")


@dataclass(frozen=True, slots=True)
class TorrentFile:
    path: str
    length: int


@dataclass(frozen=True, slots=True)
class TorrentMeta:
    name: str
    files: tuple[TorrentFile, ...]
    total_size: int
    info_hash: str
    private: bool
    is_multi: bool = False


def _decode_name(raw: object, fallback: str = "") -> str:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", errors="replace")
    elif isinstance(raw, str):
        text = raw
    else:
        return fallback
    text = text.replace("\x00", "").strip()
    return text or fallback


def _safe_path(parts: object) -> str:
    if not isinstance(parts, list) or not parts:
        raise BencodeError("文件路径为空")
    cleaned: list[str] = []
    for part in parts:
        name = _decode_name(part)
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            raise BencodeError(f"非法文件路径片段: {name!r}")
        cleaned.append(name)
    return "/".join(cleaned)


def parse_torrent(data: bytes) -> TorrentMeta:
    if not isinstance(data, (bytes, bytearray)):
        raise BencodeError("种子内容必须是 bytes")
    data = bytes(data)
    if not data:
        raise BencodeError("种子内容为空")
    if len(data) > MAX_TORRENT_BYTES:
        raise BencodeError("种子文件超过大小限制")

    top = bdecode(data)
    if not isinstance(top, dict):
        raise BencodeError("种子顶层必须是字典")
    info = top.get(b"info")
    if not isinstance(info, dict):
        raise BencodeError("种子缺少 info")

    name = _decode_name(info.get(b"name"), fallback="torrent")
    files: list[TorrentFile] = []
    is_multi = b"files" in info
    if is_multi:
        raw_files = info[b"files"]
        if not isinstance(raw_files, list) or not raw_files:
            raise BencodeError("files 字段为空")
        if len(raw_files) > MAX_FILES:
            raise BencodeError("文件数量超过限制")
        for entry in raw_files:
            if not isinstance(entry, dict):
                raise BencodeError("文件条目格式错误")
            length = entry.get(b"length")
            if not isinstance(length, int) or length < 0:
                raise BencodeError("文件长度非法")
            files.append(TorrentFile(path=_safe_path(entry.get(b"path")), length=length))
    else:
        length = info.get(b"length")
        if not isinstance(length, int) or length < 0:
            raise BencodeError("单文件种子缺少 length")
        files.append(TorrentFile(path=name, length=length))

    total = sum(item.length for item in files)
    start, end = _info_span(data)
    digest = hashlib.sha1(data[start:end]).hexdigest()
    private_raw = info.get(b"private")
    private = private_raw in (1, b"1", True)
    return TorrentMeta(
        name=name,
        files=tuple(files),
        total_size=total,
        info_hash=digest,
        private=private,
        is_multi=is_multi,
    )


def iter_files(meta: TorrentMeta) -> Iterator[TorrentFile]:
    yield from meta.files
