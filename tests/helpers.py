"""Isolated app factory for tests. Never points at production SQLite."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app


def make_settings(tmp_path: Path, **overrides) -> Settings:
    payload = {
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'team48.db').as_posix()}",
        "secret_key": "test-secret-key",
        "admin_username": "hixz12",
        "admin_password": "test-password",
        "session_cookie_secure": False,
        "auto_reauth_enabled": False,
        "auto_rotate_enabled": False,
        "force_refill": False,
    }
    payload.update(overrides)
    return Settings(**payload)


@contextmanager
def make_client(tmp_path: Path, **overrides) -> Iterator[TestClient]:
    app = create_app(make_settings(tmp_path, **overrides))
    with TestClient(app) as client:
        yield client
