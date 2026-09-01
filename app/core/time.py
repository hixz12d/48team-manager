"""Timezone helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytz


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def zone(name: str):
    return pytz.timezone(name)


def isoformat(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()
