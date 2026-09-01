"""Query APIs. Reads never call OpenAI, Sub2API, or Playwright."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.queries import console as console_query
from app.application.queries.identity import identity_audit_query
from app.application.settings import save_console_settings
from app.web.deps import require_admin
from app.web.schemas.settings import SettingsPatch


def build_api_router(get_db) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["api"])

    @router.get("/overview")
    async def overview(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.overview(db)

    @router.get("/workspaces")
    async def workspaces(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.workspaces(db)

    @router.get("/accounts")
    async def accounts(
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
        purpose: str = Query("all"),
        include_archived: bool = Query(False),
    ) -> dict:
        return await console_query.accounts(db, purpose=purpose, include_archived=include_archived)

    @router.get("/identity/audit")
    async def identity_audit(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await identity_audit_query(db)

    @router.get("/operations")
    async def operations(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.operations(db)

    @router.get("/resources/phones")
    async def phones(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.phones(db)

    @router.get("/resources/hme")
    async def hme(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.hme(db)

    @router.get("/resources/proxies")
    async def proxies(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.proxies(db)

    @router.get("/settings")
    async def settings_view(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        return await console_query.settings_view(db)

    @router.patch("/settings")
    async def settings_update(
        payload: SettingsPatch,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        try:
            return await save_console_settings(db, payload)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return router
