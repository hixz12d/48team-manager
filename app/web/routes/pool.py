"""Standby pool API. Routes only validate and map errors; business logic lives in the services."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import pool_join, standby_pool
from app.web.deps import require_admin
from app.web.schemas.pool import PoolImportRequest, PoolJoinRequest

CONFLICT_CODES = {"operation_conflict", "browser_busy", "rotation_unresolved", "team_full", "not_joinable"}


def _raise_for(result: dict, fallback: str) -> None:
    if result.get("ok") is not False:
        return
    code = str(result.get("error_code") or "request_failed")
    detail: dict = {"message": str(result.get("error") or fallback), "error_code": code}
    if result.get("operation_id"):
        detail["operation_id"] = result["operation_id"]
    if code == "not_found":
        status_code = 404
    elif code in CONFLICT_CODES:
        status_code = 409
    else:
        status_code = 400
    raise HTTPException(status_code=status_code, detail=detail)


def build_pool_router(get_db) -> APIRouter:
    router = APIRouter(prefix="/api/pool", tags=["pool"])

    @router.get("")
    async def list_pool(_: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        result = await standby_pool.list_entries(db)
        _raise_for(result, "读取号池失败")
        return result

    @router.post("/import")
    async def import_pool(
        payload: PoolImportRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await standby_pool.import_emails(db, payload.text)
        _raise_for(result, "导入失败")
        return result

    @router.get("/recommend")
    async def recommend_pool(
        entry_id: int = Query(...),
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await standby_pool.recommend_workspaces(db, entry_id)
        _raise_for(result, "读取候选团队失败")
        return result

    @router.post("/{entry_id}/recheck")
    async def recheck_pool_entry(entry_id: int, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        result = await standby_pool.recheck_mailbox(db, entry_id)
        _raise_for(result, "重新检测失败")
        return result

    @router.post("/{entry_id}/join", status_code=202)
    async def join_pool_entry(
        entry_id: int,
        payload: PoolJoinRequest,
        _: dict = Depends(require_admin),
        db: AsyncSession = Depends(get_db),
    ) -> dict:
        result = await pool_join.start_pool_join(
            db,
            entry_id,
            workspace_id=payload.workspace_id,
            role=payload.role,
            seat_intent=payload.seat_intent,
            replace_email=payload.replace_email,
        )
        _raise_for(result, "拉入没有启动")
        return result

    @router.post("/{entry_id}/continue", status_code=202)
    async def continue_pool_entry(entry_id: int, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        result = await pool_join.continue_pool_join(db, entry_id)
        _raise_for(result, "继续拉入没有启动")
        return result

    @router.delete("/{entry_id}")
    async def remove_pool_entry(entry_id: int, _: dict = Depends(require_admin), db: AsyncSession = Depends(get_db)) -> dict:
        result = await standby_pool.remove_entry(db, entry_id)
        _raise_for(result, "移出号池失败")
        return result

    return router
