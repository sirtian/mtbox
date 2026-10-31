from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import notifier as notifier_module
from app.services import settings_store
from app.services.notifier import Notifier, notify_config_error
from tests.helpers import make_db, make_settings


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


@pytest.fixture
def env(tmp_path: Path):
    settings = make_settings(tmp_path, tmp_path / "downloads", tmp_path / "media")
    db = make_db(settings)
    return settings, db


def configure(db, config: dict) -> None:
    settings_store.set_values(db, {"notify_config": json.dumps(config)})


def test_no_config_sends_nothing(env) -> None:
    _, db = env
    assert Notifier(db).send("标题", "内容") == ()


def test_wecom_success(env, monkeypatch) -> None:
    _, db = env
    configure(db, {"wecom": "https://example.invalid/hook?key=abc"})
    calls: list[dict] = []

    def fake_post(url, *, json=None, data=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return FakeResponse(200, {"errcode": 0})

    monkeypatch.setattr(notifier_module.httpx, "post", fake_post)
    assert Notifier(db).send("标题", "内容", level="warn") == ("wecom",)
    assert calls[0]["url"].startswith("https://example.invalid")
    assert "[WARN]" in calls[0]["json"]["text"]["content"]
    assert calls[0]["timeout"] == 5.0


def test_wecom_error_code_fails(env, monkeypatch) -> None:
    _, db = env
    configure(db, {"wecom": "https://example.invalid/hook"})
    monkeypatch.setattr(
        notifier_module.httpx, "post", lambda *a, **k: FakeResponse(200, {"errcode": 40001})
    )
    assert Notifier(db).send("标题", "内容") == ()


def test_serverchan_success(env, monkeypatch) -> None:
    _, db = env
    configure(db, {"serverchan": "https://sctapi.ftqq.com/xxx.send"})
    calls: list[dict] = []

    def fake_post(url, *, json=None, data=None, timeout=None):
        calls.append({"data": data})
        return FakeResponse(200, {"code": 0})

    monkeypatch.setattr(notifier_module.httpx, "post", fake_post)
    assert Notifier(db).send("标题", "内容") == ("serverchan",)
    assert calls[0]["data"]["title"] == "标题"


def test_channel_exception_is_swallowed(env, monkeypatch) -> None:
    _, db = env
    configure(db, {"wecom": "https://example.invalid/hook"})

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(notifier_module.httpx, "post", boom)
    assert Notifier(db).send("标题", "内容") == ()


def test_smtp_success(env, monkeypatch) -> None:
    _, db = env
    configure(
        db,
        {
            "smtp": {
                "host": "smtp.example.invalid",
                "port": 465,
                "user": "bot@example.invalid",
                "password": "secret",
                "to": "me@example.invalid",
            }
        },
    )
    sent: list[object] = []

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port

        def login(self, user, password):
            sent.append(("login", user, password))

        def send_message(self, message):
            sent.append(message)

        def quit(self):
            sent.append("quit")

        def close(self):
            pass

    monkeypatch.setattr(notifier_module.smtplib, "SMTP_SSL", FakeSMTP)
    assert Notifier(db).send("标题", "内容") == ("smtp",)
    message = [item for item in sent if not isinstance(item, tuple)][0]
    assert message["Subject"] == "[mtbox] 标题"
    assert message["To"] == "me@example.invalid"


def test_notify_config_validation() -> None:
    assert notify_config_error("{}") is None
    assert notify_config_error("not json")
    assert notify_config_error("[]")
    assert notify_config_error('{"wecom": 1}')
    assert notify_config_error('{"smtp": "x"}')
