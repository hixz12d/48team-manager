"""Create the new schema without touching a legacy production database file."""

from __future__ import annotations

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
    SystemSetting,
    Workspace,
    WorkspaceMembership,
)


async def bootstrap_schema(engine: AsyncEngine) -> None:
    await init_db(engine)
