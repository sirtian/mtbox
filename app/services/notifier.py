"""Notification channels configured in ``notify_config`` (design §8.8).

Failures never propagate: notifications are best-effort and must not block
grabbing, organizing or cleanup.
"""

from __future__ import annotations

import json
import logging
import smtplib
from email.message import EmailMessage
from typing import Any

import httpx

from ..db import Database
from . import settings_store

log = logging.getLogger("mtbox.notifier")

TIMEOUT = 5.0


class Notifier:
    def __init__(self, db: Database) -> None:
        self.db = db

    def config(self) -> dict[str, Any]:
        raw = settings_store.get_json(self.db, "notify_config", {}) or {}
        return raw if isinstance(raw, dict) else {}

    def send(self, title: str, message: str, *, level: str = "info") -> tuple[str, ...]:
        config = self.config()
        if not config:
            return ()
        text = f"[{level.upper()}] {title}\n{message}" if level != "info" else f"{title}\n{message}"
        sent: list[str] = []

        wecom = config.get("wecom")
        if isinstance(wecom, str) and wecom.strip():
            if self._guard("wecom", self._send_wecom, wecom.strip(), text):
                sent.append("wecom")

        serverchan = config.get("serverchan")
        if isinstance(serverchan, str) and serverchan.strip():
            if self._guard("serverchan", self._send_serverchan, serverchan.strip(), title, text):
                sent.append("serverchan")

        smtp = config.get("smtp")
        if isinstance(smtp, dict) and smtp.get("host"):
            if self._guard("smtp", self._send_smtp, smtp, title, text):
                sent.append("smtp")

        if not sent:
            log.warning("通知未发送（未配置或全部渠道失败）: %s", title)
        return tuple(sent)

    @staticmethod
    def _guard(name: str, fn: Any, *args: Any) -> bool:
        try:
            return bool(fn(*args))
        except Exception as exc:  # noqa: BLE001 - never break the caller
            log.warning("通知渠道 %s 失败: %s", name, exc.__class__.__name__)
            return False

    @staticmethod
    def _send_wecom(url: str, text: str) -> bool:
        response = httpx.post(
            url,
            json={"msgtype": "text", "text": {"content": text}},
            timeout=TIMEOUT,
        )
        if response.status_code >= 400:
            return False
        try:
            payload = response.json()
        except ValueError:
            return True
        return int(payload.get("errcode", 0)) == 0

    @staticmethod
    def _send_serverchan(url: str, title: str, text: str) -> bool:
        response = httpx.post(
            url,
            data={"title": title, "desp": text},
            timeout=TIMEOUT,
        )
        return response.status_code < 400

    @staticmethod
    def _send_smtp(config: dict[str, Any], title: str, text: str) -> bool:
        host = str(config.get("host") or "").strip()
        if not host:
            return False
        port = int(config.get("port") or 465)
        user = str(config.get("user") or "").strip()
        password = str(config.get("password") or "")
        recipient = config.get("to")
        recipients = (
            [item.strip() for item in recipient.split(",") if item.strip()]
            if isinstance(recipient, str)
            else [str(item).strip() for item in recipient if str(item).strip()]
            if isinstance(recipient, list)
            else []
        )
        if not recipients:
            return False

        message = EmailMessage()
        message["Subject"] = f"[mtbox] {title}"
        message["From"] = str(config.get("from") or user or "mtbox@localhost")
        message["To"] = ", ".join(recipients)
        message.set_content(text)

        if port == 465:
            client: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=TIMEOUT)
        else:
            client = smtplib.SMTP(host, port, timeout=TIMEOUT)
        try:
            if port != 465:
                client.ehlo()
                try:
                    client.starttls()
                    client.ehlo()
                except smtplib.SMTPNotSupportedError:
                    pass
            if user:
                client.login(user, password)
            client.send_message(message)
        finally:
            try:
                client.quit()
            except Exception:  # noqa: BLE001
                client.close()
        return True


def notify_config_error(raw: str) -> str | None:
    """Validate the settings form value; return an error message or None."""
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        return f"通知配置不是合法 JSON: {exc}"
    if not isinstance(value, dict):
        return "通知配置必须是 JSON 对象"
    for key in ("wecom", "serverchan"):
        if key in value and not isinstance(value[key], str):
            return f"{key} 必须是字符串 URL"
    smtp = value.get("smtp")
    if smtp is not None and not isinstance(smtp, dict):
        return "smtp 必须是对象"
    return None
