"""Query APIs. Reads never call OpenAI, Sub2API, or Playwright."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.queries.identity import accounts_query, identity_audit_query, overview_query, workspaces_query
from app.web.deps import require_admin


def build_api_router(get_db) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/overview")
    async def overview(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await overview_query(db)

    @router.get("/workspaces")
    async def workspaces(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await workspaces_query(db)

    @router.get("/accounts")
    async def accounts(
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        purpose: str = Query("all"),
        include_archived: bool = Query(False),
    ) -> dict:
        return await accounts_query(db, purpose=purpose, include_archived=include_archived)

    @router.get("/identity/audit")
    async def identity_audit(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await identity_audit_query(db)

    @router.get("/operations")
    async def operations(_: dict = Depends(require_admin)) -> dict:
        return {"items": [], "next_cursor": None}

    @router.get("/resources/phones")
    async def phones(_: dict = Depends(require_admin)) -> dict:
        return {"items": [], "next_cursor": None}

    @router.get("/resources/hme")
    async def hme(_: dict = Depends(require_admin)) -> dict:
        return {"items": [], "next_cursor": None}

    @router.get("/resources/proxies")
    async def proxies(_: dict = Depends(require_admin)) -> dict:
        return {"items": [], "next_cursor": None}

    @router.get("/settings")
    async def settings_view(_: dict = Depends(require_admin)) -> dict:
        return {
            "connections": {"sub2api": {"configured": False}, "hme": {"configured": False}},
            "automation": {
                "official_quota_probe": True,
                "auto_reauth": False,
                "auto_rotate": False,
                "force_refill": False,
            },
            "secrets": {"sub2api_api_key": "••••••", "hme_token": "••••••"},
        }

    return router
