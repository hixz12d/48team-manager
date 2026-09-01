"""Admin login against system_settings."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.security import hash_password, verify_password
from app.persistence.models.settings import SystemSetting

ADMIN_PASSWORD_KEY = "admin_password_hash"


async def get_setting(db: AsyncSession, key: str) -> SystemSetting | None:
    result = await db.execute(select(SystemSetting).where(SystemSetting.key == key))
    return result.scalar_one_or_none()


async def upsert_setting(
    db: AsyncSession,
    key: str,
    value: str,
    description: str | None = None,
) -> SystemSetting:
    row = await get_setting(db, key)
    if row is None:
        row = SystemSetting(key=key, value=value, description=description)
        db.add(row)
    else:
        row.value = value
        if description is not None:
            row.description = description
    await db.commit()
    await db.refresh(row)
    return row


async def initialize_admin_password(db: AsyncSession, settings: Settings) -> None:
    existing = await get_setting(db, ADMIN_PASSWORD_KEY)
    if existing and existing.value:
        return
    await upsert_setting(
        db,
        ADMIN_PASSWORD_KEY,
        hash_password(settings.admin_password),
        "Administrator password hash",
    )


async def verify_admin_login(db: AsyncSession, settings: Settings, username: str, password: str) -> bool:
    expected = (settings.admin_username or "").strip()
    provided = (username or "").strip()
    if not expected or provided != expected:
        return False
    await initialize_admin_password(db, settings)
    row = await get_setting(db, ADMIN_PASSWORD_KEY)
    if not row or not row.value:
        return False
    return verify_password(password, row.value)


async def change_admin_password(
    db: AsyncSession,
    old_password: str,
    new_password: str,
) -> bool:
    row = await get_setting(db, ADMIN_PASSWORD_KEY)
    if not row or not row.value or not verify_password(old_password, row.value):
        return False
    row.value = hash_password(new_password)
    await db.commit()
    return True
