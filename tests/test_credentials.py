from __future__ import annotations

import logging
from pathlib import Path

import httpx

from app.clients.mteam import MTeamClient
from app.clients.qbittorrent import QBittorrentClient
from app.redaction import RedactFilter, register_secrets
from app.services import credentials, settings_store
from app.services.ratelimit import ApiLimiter
from tests.helpers import make_db, make_settings


def test_saved_overrides_take_precedence_over_env(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, tmp_path / "downloads", tmp_path / "media",
        mt_api_key="env-key", qb_password="env-password",
    )
    db = make_db(settings)
    credentials.save(db, credentials.MT_API_KEY, "db-key")
    credentials.save(db, credentials.QB_PASSWORD, "db-password")

    effective = credentials.apply_overrides(settings, db)

    assert settings.mt_api_key == "db-key"
    assert settings.qb_password == "db-password"
    assert effective == {credentials.MT_API_KEY: "db-key", credentials.QB_PASSWORD: "db-password"}
    assert credentials.status(settings, db, credentials.MT_API_KEY).source == "settings"


def test_clear_restores_env_source(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, tmp_path / "downloads", tmp_path / "media",
        mt_api_key="env-key", qb_password="env-password",
    )
    db = make_db(settings)
    base = credentials.env_values(settings)
    credentials.save(db, credentials.MT_API_KEY, "db-key")
    credentials.apply_overrides(settings, db)

    credentials.clear(db, credentials.MT_API_KEY)
    for key, value in base.items():
        setattr(settings, key, value)
    credentials.apply_overrides(settings, db)

    assert settings.mt_api_key == "env-key"
    assert settings_store.get_value(db, credentials.MT_API_KEY) is None
    assert credentials.status(settings, db, credentials.MT_API_KEY).source == "env"


def test_validate_secrets() -> None:
    assert credentials.validate("", credentials.MT_API_KEY) is None
    assert credentials.validate("abc123", credentials.MT_API_KEY) is None
    assert credentials.validate("a" * 513, credentials.MT_API_KEY) is not None
    assert credentials.validate("a\nb", credentials.QB_PASSWORD) is not None
    assert credentials.validate("has space", credentials.MT_API_KEY) is not None


def test_redaction_filter_masks_newly_registered_secret() -> None:
    secret = "rotated-secret-value"
    register_secrets(secret)
    record = logging.LogRecord(
        "mtbox.test", logging.INFO, __file__, 1, "login failed for %s", (secret,), None
    )
    assert RedactFilter().filter(record) is True
    assert secret not in record.getMessage()
    assert "***" in record.getMessage()


def test_mteam_client_updates_header(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "media")
    db = make_db(settings)
    client = MTeamClient(
        settings,
        ApiLimiter(settings, db),
        client=httpx.Client(base_url="https://example.invalid"),
        download_client=httpx.Client(base_url="https://example.invalid"),
    )
    try:
        client.set_api_key("rotated-key")
        assert settings.mt_api_key == "rotated-key"
        assert client._client.headers["x-api-key"] == "rotated-key"
    finally:
        client.close()


def test_qbittorrent_set_password_forces_relogin(tmp_path: Path) -> None:
    settings = make_settings(
        tmp_path, tmp_path / "downloads", tmp_path / "media",
        qb_username="admin", qb_password="old",
    )
    bodies: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(request.content)
        return httpx.Response(
            200, text="Ok.", headers={"set-cookie": "SID=session-cookie; path=/"}
        )

    http_client = httpx.Client(
        base_url="http://qb.local:8080", transport=httpx.MockTransport(handler)
    )
    client = QBittorrentClient(settings, client=http_client)
    try:
        client.login()
        assert b"password=old" in bodies[0]
        client._client.cookies.set("SID", "old-session")
        client.set_password("new-password")
        assert settings.qb_password == "new-password"
        assert client._logged_in is False
        assert not client._client.cookies
        client.login()
        assert b"password=new-password" in bodies[-1]
    finally:
        client.close()
