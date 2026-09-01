"""Phase 8：独立 /admin/v2。不改旧后台，不依赖全量 dashboard refresh。"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.services.admin_v2 import AccountVersionConflict, admin_v2_service

router = APIRouter(prefix="/admin/v2", tags=["admin-v2"])


class AccountPatchRequest(BaseModel):
    version: int = Field(..., ge=1)
    local_purpose: Optional[str] = None
    operational_state: Optional[str] = None


class AccountDeleteRequest(BaseModel):
    version: int = Field(..., ge=1)


class SettingsUpdateRequest(BaseModel):
    sub2api_base_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None
    hme_base_url: Optional[str] = None
    hme_service_token: Optional[str] = None
    hme_account_id: Optional[str] = None
    official_quota_probe_enabled: Optional[bool] = None
    official_quota_probe_interval_minutes: Optional[int] = None
    official_quota_probe_stagger_minutes: Optional[int] = None
    auto_reauth_enabled: Optional[bool] = None
    auto_rotate_enabled: Optional[bool] = None


def _json(payload: Any, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=payload, status_code=status_code)


@router.get("/", response_class=HTMLResponse)
async def v2_home(request: Request, current_user: dict = Depends(require_admin)):
    from app.main import templates

    return templates.TemplateResponse(
        request,
        "admin/v2/index.html",
        {
            "request": request,
            "user": current_user,
            "active_page": "v2",
        },
    )


@router.get("/api/board")
async def v2_board(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return _json(await admin_v2_service.build_board(db))


@router.get("/api/accounts/{account_id}")
async def v2_account(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    payload = await admin_v2_service.get_account(db, account_id)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    return _json(payload)


@router.patch("/api/accounts/{account_id}")
async def v2_patch_account(
    account_id: int,
    body: AccountPatchRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        payload = await admin_v2_service.patch_account(
            db,
            account_id,
            version=body.version,
            local_purpose=body.local_purpose,
            operational_state=body.operational_state,
        )
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    except AccountVersionConflict as exc:
        return _json(
            {
                "error": "version_conflict",
                "account_id": exc.account_id,
                "version": exc.current_version,
            },
            status_code=status.HTTP_409_CONFLICT,
        )
    return _json(payload)


@router.post("/api/accounts/{account_id}/archive")
async def v2_archive_account(
    account_id: int,
    body: AccountDeleteRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        payload = await admin_v2_service.archive_account(db, account_id, version=body.version)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    except AccountVersionConflict as exc:
        return _json(
            {
                "error": "version_conflict",
                "account_id": exc.account_id,
                "version": exc.current_version,
            },
            status_code=status.HTTP_409_CONFLICT,
        )
    return _json(payload)


@router.post("/api/accounts/{account_id}/refresh-official")
async def v2_refresh_official(
    account_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        payload = await admin_v2_service.refresh_official(db, account_id)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="账号不存在")
    return _json(payload)


@router.get("/api/operations")
async def v2_operations(
    limit: int = Query(80, ge=1, le=200),
    email: str = "",
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return _json({"items": await admin_v2_service.list_operations(db, limit=limit, email=email)})


@router.get("/api/operations/{public_id}")
async def v2_operation(
    public_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    payload = await admin_v2_service.get_operation(db, public_id)
    if payload is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="任务不存在")
    return _json(payload)


@router.get("/api/resources")
async def v2_resources(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return _json(await admin_v2_service.list_resources(db))


@router.get("/api/resources/phones/{phone_id}/attempts")
async def v2_phone_attempts(
    phone_id: int,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return _json({"items": await admin_v2_service.list_phone_attempts(db, phone_id, limit=limit)})


@router.get("/api/settings")
async def v2_get_settings(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return _json(await admin_v2_service.get_settings(db))


@router.post("/api/settings")
async def v2_update_settings(
    body: SettingsUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    payload = body.model_dump(exclude_unset=True)
    return _json(await admin_v2_service.update_settings(db, payload))
