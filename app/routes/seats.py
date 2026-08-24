"""自用子号池 / 拉人 / 踢人 / 轮转接口。"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.dependencies.auth import require_admin
from app.services.child_accounts import child_account_service
from app.services.onboard import onboard_service
from app.services.sub2api import sub2api_service
from app.services.team import team_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["seats"])


async def attach_live_members(
    db: AsyncSession,
    cards: List[Dict[str, Any]],
    status_index: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    status_index = status_index or {}
    enriched: List[Dict[str, Any]] = []
    for card in cards:
        item = dict(card)
        live = await team_service.get_team_members(int(card["id"]), db)
        local_by_email = {
            str(child.get("email") or "").lower(): child
            for child in (card.get("active_children") or [])
        }
        live_members = []
        if live.get("success"):
            for member in live.get("members") or []:
                email = str(member.get("email") or "").strip().lower()
                local = local_by_email.get(email) or {}
                remote = status_index.get(email) or {}
                live_members.append({
                    "email": email or member.get("email"),
                    "user_id": member.get("user_id"),
                    "role": member.get("role") or "",
                    "status": member.get("status") or "joined",
                    "local_status": local.get("status") or "未入库",
                    "joined_at": member.get("added_at") or local.get("joined_at"),
                    "remaining_days": local.get("remaining_days"),
                    "quota_label": remote.get("quota_label") or "",
                    "schedule_label": remote.get("schedule_label") or "",
                    "tone": remote.get("tone") or "muted",
                    "sub2api_account_id": remote.get("id") or local.get("sub2api_account_id"),
                    "in_pool": bool(local),
                })
            item["live_error"] = None
        else:
            item["live_error"] = live.get("error") or "读取当前 Team 成员失败"
        item["live_members"] = live_members
        enriched.append(item)
    return enriched


async def load_sub2api_dashboard(db: AsyncSession, *, force: bool = False) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    status = await sub2api_service.dashboard_status(db, force=force)
    cards = await attach_live_members(
        db,
        await child_account_service.dashboard_cards(db),
        sub2api_service.index_status_by_email(status.get("boxes") or []),
    )
    sub2api_service.annotate_rotation(status.get("boxes") or [], cards)
    return cards, status


class OnboardRequest(BaseModel):
    team_id: int
    email: str = Field(..., description="邮箱，或 email----pickup_url；只填 iCloud 别名时走 Cloudflare 读码")
    phone: str = Field("", description="+1xxxx----https://api668.com/sms/by_key?key=...")
    proxy: str = Field("", description="子号静态 ISP，不填则用母号 ISP")
    password: str = ""
    reuse_existing: bool = True


class KickRequest(BaseModel):
    team_id: int
    email: str
    user_id: Optional[str] = None


class RotateRequest(BaseModel):
    team_id: int
    email: str = ""
    phone: str = ""
    proxy: str = ""
    child_id: Optional[int] = None


class ChildUpdateRequest(BaseModel):
    phone: Optional[str] = None
    sms_url: Optional[str] = None
    proxy: Optional[str] = None
    password: Optional[str] = None
    mail_raw: Optional[str] = None
    cycle_days: Optional[int] = None


class ProxyCheckRequest(BaseModel):
    proxy: str = Field("", description="要检测的代理")
    team_id: Optional[int] = None
    child_id: Optional[int] = None


@router.get("/seats", response_class=HTMLResponse)
async def seats_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.main import templates
    from app.routes.admin import build_admin_base_context

    context = await build_admin_base_context(request, db, current_user, "seats")
    cards, sub2api_status = await load_sub2api_dashboard(db)
    context.update({
        "cards": cards,
        "children": [child_account_service.serialize(item) for item in await child_account_service.list_accounts(db)],
        "stats": await child_account_service.stats(db),
        "sub2api_status": sub2api_status,
    })
    return templates.TemplateResponse(request, "admin/seats/index.html", context)


@router.get("/seats/list")
async def seats_list(
    status: Optional[str] = None,
    team_id: Optional[int] = None,
    search: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    children = await child_account_service.list_accounts(db, status=status, team_id=team_id, search=search)
    cards, sub2api_status = await load_sub2api_dashboard(db)
    return {
        "success": True,
        "children": [child_account_service.serialize(item) for item in children],
        "stats": await child_account_service.stats(db),
        "cards": cards,
        "sub2api_status": sub2api_status,
    }


@router.get("/seats/sub2api-status")
async def seats_sub2api_status(
    force: bool = False,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    _cards, status = await load_sub2api_dashboard(db, force=force)
    return {"success": True, **status}


@router.post("/seats/onboard")
async def seats_onboard(
    payload: OnboardRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        result = await onboard_service.invite_and_onboard(
            db,
            team_id=payload.team_id,
            email_line=payload.email,
            phone_line=payload.phone,
            proxy=payload.proxy,
            password=payload.password,
            reuse_existing=payload.reuse_existing,
        )
        status_code = 200 if result.get("success") else 400
        return JSONResponse(status_code=status_code, content=result)
    except Exception as exc:
        logger.exception("拉人失败")
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})


@router.post("/seats/kick")
async def seats_kick(
    payload: KickRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        result = await onboard_service.kick_to_standby(
            db,
            team_id=payload.team_id,
            email=payload.email,
            user_id=payload.user_id,
        )
        status_code = 200 if result.get("success") else 400
        return JSONResponse(status_code=status_code, content=result)
    except Exception as exc:
        logger.exception("踢人失败")
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})


@router.post("/seats/rotate")
async def seats_rotate(
    payload: RotateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    try:
        result = await onboard_service.rotate_one(
            db,
            team_id=payload.team_id,
            email_line=payload.email,
            phone_line=payload.phone,
            proxy=payload.proxy,
            child_id=payload.child_id,
        )
        status_code = 200 if result.get("success") else 400
        return JSONResponse(status_code=status_code, content=result)
    except Exception as exc:
        logger.exception("轮转失败")
        return JSONResponse(status_code=400, content={"success": False, "error": str(exc)})


@router.post("/seats/{child_id}/update")
async def seats_update(
    child_id: int,
    payload: ChildUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    child = await child_account_service.get_by_id(db, child_id)
    if not child:
        return JSONResponse(status_code=404, content={"success": False, "error": "子号不存在"})
    await child_account_service.upsert_from_input(
        db,
        email=child.email,
        password=payload.password or "",
        mail_raw=payload.mail_raw or "",
        phone=payload.phone or "",
        sms_url=payload.sms_url or "",
        proxy=payload.proxy or "",
        cycle_days=payload.cycle_days,
    )
    await db.commit()
    return {"success": True, "child": child_account_service.serialize(child)}


@router.post("/seats/{child_id}/delete")
async def seats_delete(
    child_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    child = await child_account_service.get_by_id(db, child_id)
    if not child:
        return JSONResponse(status_code=404, content={"success": False, "error": "子号不存在"})
    if child.status in {"invited", "active"}:
        return JSONResponse(status_code=400, content={"success": False, "error": "请先踢出 Team，再删除子号"})
    await child_account_service.mark_deleted(db, child)
    await child_account_service.record_event(
        db,
        email=child.email,
        action="delete",
        child_id=child.id,
        success=True,
        detail="local delete only",
    )
    await db.commit()
    return {"success": True, "message": f"{child.email} 已从本地子号池删除"}


@router.post("/proxy/check")
async def proxy_check(
    payload: ProxyCheckRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services.proxy_check import check_proxy
    from app.models import Team

    proxy = (payload.proxy or "").strip()
    if not proxy and payload.team_id:
        team = await db.get(Team, payload.team_id)
        proxy = (team.proxy if team else "") or ""
    if not proxy and payload.child_id:
        child = await child_account_service.get_by_id(db, payload.child_id)
        proxy = (child.proxy if child else "") or ""
    result = await check_proxy(proxy)
    result["success"] = True
    return result


@router.post("/proxy/check-all")
async def proxy_check_all(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from sqlalchemy import select

    from app.models import Team
    from app.services.proxy_check import check_many

    teams = (await db.execute(select(Team).order_by(Team.id.asc()))).scalars().all()
    children = await child_account_service.list_accounts(db)
    items = []
    for team in teams:
        items.append({
            "id": team.id,
            "kind": "team",
            "label": team.team_name or team.email,
            "proxy": team.proxy or "",
        })
    for child in children:
        if child.proxy:
            items.append({
                "id": child.id,
                "kind": "child",
                "label": child.email,
                "proxy": child.proxy,
            })
    results = await check_many(items)
    return {"success": True, "results": results}
