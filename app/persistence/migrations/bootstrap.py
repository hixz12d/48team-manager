"""Create the new schema without touching a legacy production database file."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from app.persistence.database import init_db
from app.persistence.models import (  # noqa: F401
    Account,
    ExternalBinding,
    HmeAliasLease,
    Operation,
    OperationStep,
    PhoneAttempt,
    PhonePool,
    ProxyProfile,
    QuotaSnapshot,
    SeatVacancyEvent,
    SystemSetting,
    Workspace,
    WorkspaceMembership,
    WorkspaceOfficialMemberSnapshot,
)

PROXY_PROFILE_COLUMNS = (
    ("health_state", "VARCHAR(20) DEFAULT 'unchecked' NOT NULL"),
    ("last_success_at", "DATETIME"),
    ("latency_ms", "INTEGER"),
    ("last_error", "TEXT"),
)


async def _ensure_sqlite_columns(conn, table: str, columns: tuple[tuple[str, str], ...]) -> None:
    existing = {
        str(row[1])
        for row in (await conn.execute(text(f"PRAGMA table_info({table})"))).fetchall()
    }
    for name, ddl in columns:
        if name in existing:
            continue
        await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


async def bootstrap_schema(engine: AsyncEngine) -> None:
    await init_db(engine)
    if not str(engine.url).startswith("sqlite"):
        return
    async with engine.begin() as conn:
        await _ensure_sqlite_columns(conn, "proxy_profiles", PROXY_PROFILE_COLUMNS)
