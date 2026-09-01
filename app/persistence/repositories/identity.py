"""Identity repositories. Queries never call OpenAI or Sub2API."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership


async def list_accounts(db: AsyncSession) -> list[Account]:
    result = await db.execute(select(Account).order_by(Account.id))
    return list(result.scalars().all())


async def list_workspaces(db: AsyncSession) -> list[Workspace]:
    result = await db.execute(select(Workspace).order_by(Workspace.id))
    return list(result.scalars().all())


async def list_memberships(db: AsyncSession) -> list[WorkspaceMembership]:
    result = await db.execute(select(WorkspaceMembership).order_by(WorkspaceMembership.id))
    return list(result.scalars().all())


async def list_bindings(db: AsyncSession, provider: str | None = None) -> list[ExternalBinding]:
    stmt = select(ExternalBinding).order_by(ExternalBinding.id)
    if provider:
        stmt = stmt.where(ExternalBinding.provider == provider)
    result = await db.execute(stmt)
    return list(result.scalars().all())
