"""SystemSetting helpers. Query payloads never include raw secrets."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.settings import SystemSetting

TRUTHY = {"1", "true", "yes", "on"}


async def get_setting_value(db: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == key))).scalar_one_or_none()
    if row is None or row.value is None:
        return default
    return row.value


async def upsert_setting(db: AsyncSession, key: str, value: str, description: str | None = None) -> SystemSetting:
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == key))).scalar_one_or_none()
    if row is None:
        row = SystemSetting(key=key, value=value, description=description)
        db.add(row)
    else:
        row.value = value
        if description is not None:
            row.description = description
    await db.flush()
    return row


def as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in TRUTHY
