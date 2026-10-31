"""Log redaction for credentials that may change at runtime.

The env-based credentials are registered when logging is configured. Values
saved from the settings page are registered again after they take effect, so
old and new secrets are both masked for the lifetime of the process.
"""

from __future__ import annotations

import logging
import threading

_SECRETS: set[str] = set()
_LOCK = threading.Lock()


def register_secrets(*values: str | None) -> None:
    with _LOCK:
        _SECRETS.update(value for value in values if value)


def redact(text: str) -> str:
    with _LOCK:
        secrets = sorted(_SECRETS, key=len, reverse=True)
    for secret in secrets:
        text = text.replace(secret, "***")
    return text


class RedactFilter(logging.Filter):
    """Mask every registered credential in a log record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True
