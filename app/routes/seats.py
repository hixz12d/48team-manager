"""自用子号池 / 拉人 / 踢人 / 轮转接口。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import AsyncSessionLocal, get_db
from app.models import SeatEvent, Team
from app.dependencies.auth import require_admin
from app.services.child_accounts import child_account_service, normalize_email
from app.services.onboard import onboard_service
from app.services import onboard_jobs
from app.services.sub2api import sub2api_service
from app.services.team import team_service
from app.services.vacancy import vacancy_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["seats"])


LIVE_FETCH_CONCURRENCY = 4


async def _fetch_team_live(team_id: int) -> Dict[str, Any]:
    async with AsyncSessionLocal() as session:
        try:
            live = await team_service.get_team_members(team_id, session)
            await session.commit()
            return live
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 Team %s 成员失败: %s", team_id, exc)
            await session.rollback()
            return {"success": False, "members": [], "error": str(exc) or type(exc).__name__}


async def attach_live_members(
    db: AsyncSession,
    cards: List[Dict[str, Any]],
    status_index: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    status_index = status_index or {}
    sem = asyncio.Semaphore(LIVE_FETCH_CONCURRENCY)

    async def bound(card: Dict[str, Any]) -> tuple[int, Dict[str, Any]]:
        team_id = int(card["id"])
        async with sem:
            return team_id, await _fetch_team_live(team_id)

    fetched = dict(await asyncio.gather(*(bound(card) for card in cards))) if cards else {}
    enriched: List[Dict[str, Any]] = []
    for card in cards:
        item = dict(card)
        live = fetched.get(int(card["id"])) or {"success": False, "members": [], "error": "未读取"}
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
                    "account_user_id": member.get("account_user_id"),
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
            team = await db.get(Team, int(card["id"]))
            if team:
                await child_account_service.sync_with_live_members(db, team, live.get("members") or [])
        else:
            item["live_error"] = live.get("error") or "读取当前 Team 成员失败"
        item["live_members"] = live_members
        enriched.append(item)
    return enriched


ROTATION_EVENT_ACTIONS = ("invite", "register", "reregister", "kick", "rotate")


async def attach_rotation_events(
    db: AsyncSession,
    cards: List[Dict[str, Any]],
    *,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    today = (now or get_now()).date()
    start = datetime.combine(today, datetime.min.time())
    result = await db.execute(
        select(SeatEvent).where(
            SeatEvent.success.is_(True),
            SeatEvent.action.in_(ROTATION_EVENT_ACTIONS),
            SeatEvent.created_at >= start,
        )
    )
    by_team: Dict[int, set[str]] = {}
    for event in result.scalars().all():
        if not event.team_id:
            continue
        email = normalize_email(event.email)
        if email:
            by_team.setdefault(int(event.team_id), set()).add(email)
    for card in cards:
        owner = normalize_email(card.get("email"))
        emails = {item for item in by_team.get(int(card["id"]), set()) if item and item != owner}
        card["rotation_emails"] = sorted(emails)
        card["rotation_count"] = len(emails)
    return cards


async def load_local_cards(db: AsyncSession) -> List[Dict[str, Any]]:
    cards = await vacancy_service.attach_to_cards(
        db,
        await child_account_service.dashboard_cards(db),
    )
    await attach_rotation_events(db, cards)
    return cards


async def load_sub2api_dashboard(
    db: AsyncSession,
    *,
    force: bool = False,
    live: bool = False,
    costs: bool = False,
    allow_network: bool = True,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    status = await sub2api_service.dashboard_status(db, force=force, allow_network=allow_network)
    cards = await load_local_cards(db)
    if live:
        cards = await attach_live_members(
            db,
            cards,
            sub2api_service.index_status_by_email(status.get("boxes") or []),
        )
        from app.services.team_view import attach_operational_view
        attach_operational_view(cards)
        await db.commit()
    if costs and status.get("boxes"):
        await sub2api_service.attach_usage_costs(db, status.get("boxes") or [], force=force)
        status.update(sub2api_service.annotate_cost_totals(status.get("boxes") or []))
    sub2api_service.annotate_rotation(status.get("boxes") or [], cards)
    return cards, status


class OnboardRequest(BaseModel):
    team_id: int
    email: str = Field(..., description="邮箱，或 email----pickup_url；只填 iCloud 别名时走 Cloudflare 读码")
    phone: str = Field("", description="+1xxxx----https://api668.com/sms/by_key?key=...")
    proxy: str = Field("", description="子号静态 ISP，不填则用母号 ISP")
    password: str = ""
    reuse_existing: bool = True
    skip_invite: bool = False
    force: bool = False


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
    force_refill: bool = False


class VacancyClearRequest(BaseModel):
    team_id: int


class RotationCountRequest(BaseModel):
    team_id: int
    count: int = Field(..., ge=0, le=99)


class ReregisterRequest(BaseModel):
    child_id: Optional[int] = None
    team_id: Optional[int] = None
    email: str = ""
    phone: str = ""
    proxy: str = ""
    force: bool = False


class ReconcileApplyRequest(BaseModel):
    team_id: int


class FixAccountIdRequest(BaseModel):
    child_id: Optional[int] = None
    team_id: Optional[int] = None
    email: str = ""
    push: bool = True

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
    cards, sub2api_status = await load_sub2api_dashboard(db, allow_network=False)
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
    cards, sub2api_status = await load_sub2api_dashboard(db, allow_network=False)
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
    _cards, status = await load_sub2api_dashboard(db, force=force, costs=True)
    return {"success": True, **status}


@router.get("/seats/live")
async def seats_live(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    cards, _status = await load_sub2api_dashboard(db, live=True, allow_network=False)
    return {"success": True, "cards": cards}


async def _run_onboard_job(job_id: str, payload: OnboardRequest) -> None:
    async with AsyncSessionLocal() as db:
        try:
            result = await onboard_service.invite_and_onboard(
                db,
                team_id=payload.team_id,
                email_line=payload.email,
                phone_line=payload.phone,
                proxy=payload.proxy,
                password=payload.password,
                reuse_existing=payload.reuse_existing,
                skip_invite=payload.skip_invite,
                force=payload.force,
                job_id=job_id,
            )
            onboard_jobs.finish(job_id, result)
        except Exception as exc:
            logger.exception("后台拉人失败")
            onboard_jobs.finish(job_id, {"success": False, "error": str(exc), "error_code": "browser_failed"})
        finally:
            await db.close()


@router.post("/seats/onboard")
async def seats_onboard(
    payload: OnboardRequest,
    current_user: dict = Depends(require_admin),
):
    email = normalize_email(payload.email.split("----", 1)[0] if payload.email else "")
    active = onboard_jobs.active_job_for_email(email)
    if active:
        return {"success": True, "accepted": True, "job_id": active["id"], "message": "该邮箱已有进行中的拉人任务", "job": active}
    job = onboard_jobs.create_job(team_id=payload.team_id, email=email or payload.email, action="onboard")
    asyncio.create_task(_run_onboard_job(job["id"], payload))
    return {"success": True, "accepted": True, "job_id": job["id"], "message": "已开始拉人，进度会留在本页", "job": job}


@router.post("/seats/reregister")
async def seats_reregister(
    payload: ReregisterRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    child = None
    if payload.child_id:
        child = await child_account_service.get_by_id(db, payload.child_id)
    elif payload.email:
        child = await child_account_service.get_by_email(db, payload.email)
    if not child:
        return JSONResponse(status_code=404, content={"success": False, "error": "子号不存在"})
    team_id = payload.team_id or child.current_team_id or child.last_team_id
    if not team_id:
        return JSONResponse(status_code=400, content={"success": False, "error": "这个子号没有关联 Team，请先在表单里选 Team 再拉"})
    active = onboard_jobs.active_job_for_email(child.email)
    if active:
        return {"success": True, "accepted": True, "job_id": active["id"], "message": "该邮箱已有进行中的拉人任务", "job": active}
    request = OnboardRequest(
        team_id=int(team_id),
        email=payload.email or child.mail_raw or child.email,
        phone=payload.phone or ((child.phone or "") + ("----" + child.sms_url if child.sms_url else "")),
        proxy=payload.proxy or child.proxy or "",
        reuse_existing=True,
        skip_invite=child.status == "invited",
        force=payload.force,
    )
    job = onboard_jobs.create_job(team_id=int(team_id), email=child.email, action="reregister")
    asyncio.create_task(_run_onboard_job(job["id"], request))
    return {"success": True, "accepted": True, "job_id": job["id"], "message": f"开始重新注册 {child.email}", "job": job}


@router.get("/seats/jobs/{job_id}")
async def seats_job_status(
    job_id: str,
    current_user: dict = Depends(require_admin),
):
    job = onboard_jobs.get_job(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"success": False, "error": "任务不存在或进程已重启"})
    return {"success": True, "job": job}


@router.post("/seats/jobs/{job_id}/cancel")
async def seats_job_cancel(
    job_id: str,
    current_user: dict = Depends(require_admin),
):
    job = onboard_jobs.request_cancel(job_id)
    if not job:
        return JSONResponse(status_code=404, content={"success": False, "error": "任务不存在"})
    return {"success": True, "message": "已请求停止", "job": job}


@router.get("/seats/reconcile")
async def seats_reconcile(
    team_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return await onboard_service.preview_reconcile(db, team_id)


@router.post("/seats/reconcile")
async def seats_reconcile_apply(
    payload: ReconcileApplyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return await onboard_service.apply_reconcile(db, payload.team_id)


@router.post("/seats/fix-account-id")
async def seats_fix_account_id(
    payload: FixAccountIdRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    return await onboard_service.fix_child_account_id(
        db,
        child_id=payload.child_id,
        email=payload.email,
        team_id=payload.team_id,
        push=payload.push,
    )


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


@router.post("/seats/vacancy/clear")
async def seats_vacancy_clear(
    payload: VacancyClearRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    deleted = await vacancy_service.clear_team(db, payload.team_id)
    return {"success": True, "message": f"已清空 {deleted} 条席位阈值历史", "deleted": deleted}


@router.post("/seats/rotation-count")
async def seats_rotation_count(
    payload: RotationCountRequest,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    team = await db.get(Team, payload.team_id)
    if not team:
        return JSONResponse(status_code=404, content={"success": False, "error": "Team 不存在"})
    today = get_now().date().isoformat()
    team.rotation_manual_on = today
    team.rotation_manual_count = int(payload.count)
    await db.commit()
    badge = sub2api_service.rotation_badge(payload.count)
    return {
        "success": True,
        "team_id": team.id,
        "rotation_manual": True,
        "rotation_manual_on": today,
        **badge,
    }


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
            force_refill=payload.force_refill,
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
