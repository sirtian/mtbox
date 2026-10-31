from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from app.clients.qbittorrent import QBittorrentClient
from app.config import Settings
from app.errors import QBError


def make_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "data_dir": tmp_path,
        "qb_url": "http://qb.local:8080",
        "qb_username": "admin",
        "qb_password": "secret",
        "qb_save_path": "/downloads",
        "qb_category": "mtbox",
        "qb_tags": "mtbox",
    }
    values.update(overrides)
    return Settings(**values)


def make_client(
    tmp_path: Path,
    handler: Any,
    **overrides: object,
) -> tuple[QBittorrentClient, list[httpx.Request]]:
    settings = make_settings(tmp_path, **overrides)
    seen: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    client = httpx.Client(
        base_url=settings.qb_url,
        transport=httpx.MockTransport(recording_handler),
        follow_redirects=True,
    )
    return QBittorrentClient(settings, client=client), seen


def login_ok() -> httpx.Response:
    return httpx.Response(
        200,
        text="Ok.",
        headers={"set-cookie": "SID=session-cookie; path=/"},
    )


def test_login_rejects_bad_credentials(tmp_path: Path) -> None:
    client, seen = make_client(tmp_path, lambda request: httpx.Response(200, text="Fails."))
    with pytest.raises(QBError):
        client.login()
    assert len(seen) == 1
    assert seen[0].url.path == "/api/v2/auth/login"
    assert seen[0].headers["Referer"] == "http://qb.local:8080"


def test_login_requires_username(tmp_path: Path) -> None:
    client, seen = make_client(
        tmp_path, lambda request: login_ok(), qb_username=""
    )
    with pytest.raises(QBError):
        client.login()
    assert seen == []


def test_info_logs_in_lazily_and_reuses_cookie(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return login_ok()
        return httpx.Response(
            200,
            json=[
                {
                    "hash": "ABCDEF0123",
                    "name": "Some.Show.S01E01",
                    "state": "downloading",
                    "progress": 0.5,
                    "size": "1000",
                    "downloaded": 500,
                    "save_path": "/downloads/",
                    "content_path": "/downloads/Some.Show.S01E01",
                    "ratio": "0.1",
                    "seeding_time": 0,
                    "added_on": 1700000000,
                    "completion_on": 0,
                    "category": "mtbox",
                    "tags": "mtbox",
                }
            ],
        )

    client, seen = make_client(tmp_path, handler)
    torrents = client.info(["abcdef0123"])

    assert len(seen) == 2
    assert seen[0].url.path == "/api/v2/auth/login"
    assert seen[1].url.path == "/api/v2/torrents/info"
    assert seen[1].url.params["hashes"] == "abcdef0123"
    assert "SID=session-cookie" in seen[1].headers["cookie"]

    assert len(torrents) == 1
    item = torrents[0]
    assert item.hash == "abcdef0123"
    assert item.progress == 0.5
    assert item.size == 1000
    assert item.content_path == "/downloads/Some.Show.S01E01"
    assert item.ratio == 0.1
    assert item.category == "mtbox"


def test_info_empty_hashes_makes_no_request(tmp_path: Path) -> None:
    client, seen = make_client(tmp_path, lambda request: login_ok())
    assert client.info([]) == []
    assert seen == []


def test_403_triggers_single_relogin(tmp_path: Path) -> None:
    state = {"login": 0, "info": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            state["login"] += 1
            return login_ok()
        state["info"] += 1
        if state["info"] == 1:
            return httpx.Response(403, text="Forbidden")
        return httpx.Response(200, json=[])

    client, seen = make_client(tmp_path, handler)
    assert client.info(["abc"]) == []
    assert state == {"login": 2, "info": 2}
    assert len(seen) == 4


def test_ensure_category_existing_does_not_create(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return login_ok()
        return httpx.Response(200, json={"mtbox": {"name": "mtbox", "savePath": ""}})

    client, seen = make_client(tmp_path, handler)
    client.ensure_category("mtbox")
    assert [request.url.path for request in seen] == [
        "/api/v2/auth/login",
        "/api/v2/torrents/categories",
    ]


def test_ensure_category_creates_missing(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return login_ok()
        if request.url.path == "/api/v2/torrents/categories":
            return httpx.Response(200, json={})
        return httpx.Response(200, text="")

    client, seen = make_client(tmp_path, handler)
    client.ensure_category("mtbox")
    create = seen[-1]
    assert create.url.path == "/api/v2/torrents/createCategory"
    body = create.content.decode()
    assert "category=mtbox" in body


def test_add_sends_multipart_metadata(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return login_ok()
        return httpx.Response(200, text="Ok.")

    client, seen = make_client(tmp_path, handler)
    client.add(
        b"d4:infod4:name5:hello6:lengthi1eee",
        save_path="/downloads",
        category="mtbox",
        tags=("mtbox", "tv"),
        paused=True,
        seeding_time_limit_min=1440,
        ratio_limit=1.0,
    )

    add_request = seen[-1]
    assert add_request.url.path == "/api/v2/torrents/add"
    assert add_request.headers["content-type"].startswith("multipart/form-data")
    body = add_request.content
    assert b'name="savepath"' in body
    assert b"/downloads" in body
    assert b'name="category"' in body
    assert b"mtbox" in body
    assert b'name="tags"' in body
    assert b"mtbox,tv" in body
    assert b'name="autoTMM"' in body
    assert b"false" in body
    assert b'name="paused"' in body
    assert b'name="seedingTimeLimit"' in body
    assert b"1440" in body
    assert b'name="ratioLimit"' in body
    assert b"1.0" in body
    assert b'filename="mtbox.torrent"' in body


def test_add_retries_with_stopped_for_qb5(tmp_path: Path) -> None:
    state = {"add": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return login_ok()
        state["add"] += 1
        if state["add"] == 1:
            return httpx.Response(400, text="unsupported parameter: paused")
        return httpx.Response(200, text="Ok.")

    client, seen = make_client(tmp_path, handler)
    client.add(b"d4:infoe", save_path="/downloads", paused=True)

    add_requests = [request for request in seen if request.url.path.endswith("/torrents/add")]
    assert len(add_requests) == 2
    assert b'name="paused"' in add_requests[0].content
    assert b'name="stopped"' in add_requests[1].content
    assert b'name="paused"' not in add_requests[1].content


def test_add_failure_response_raises(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return login_ok()
        return httpx.Response(200, text="Fails.")

    client, _ = make_client(tmp_path, handler)
    with pytest.raises(QBError):
        client.add(b"d4:infoe", save_path="/downloads")


def test_delete_sends_hashes_and_file_flag(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return login_ok()
        return httpx.Response(200, text="")

    client, seen = make_client(tmp_path, handler)
    client.delete(["ABC", "def"], delete_files=True)
    body = seen[-1].content.decode()
    assert seen[-1].url.path == "/api/v2/torrents/delete"
    assert "hashes=abc%7Cdef" in body
    assert "deleteFiles=true" in body


def test_version(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/auth/login":
            return login_ok()
        return httpx.Response(200, text="v5.0.3\n")

    client, _ = make_client(tmp_path, handler)
    assert client.version() == "v5.0.3"


def test_network_error_wrapped(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client, _ = make_client(tmp_path, handler)
    with pytest.raises(QBError):
        client.login()
