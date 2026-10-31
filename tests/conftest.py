"""Test defaults.

A developer's local ``.env`` must not leak into unit tests: several tests
construct ``Settings`` directly and expect their explicit values to be the only
configuration.
"""

from __future__ import annotations

from app.config import Settings


def pytest_configure(config: object) -> None:
    Settings.model_config["env_file"] = None
