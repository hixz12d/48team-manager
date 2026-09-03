"""Timezone helpers."""

from __future__ import annotations

from datetime import datetime, timezone

import pytz


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def zone(name: str):
    return pytz.timezone(name)


def as_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def isoformat(dt: datetime | None) -> str | None:
    aware = as_utc(dt)
    return None if aware is None else aware.isoformat()
