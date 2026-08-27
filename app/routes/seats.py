"""自用子号池 / 拉人 / 踢人 / 轮转接口。"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.database import AsyncSessionLocal, get_db
from app.models import ChildAccount, SeatEvent, Team
from app.dependencies.auth import require_admin
from app.services.child_accounts import child_account_service, normalize_email
from app.services.sms import parse_phone_line
from app.services.onboard import onboard_service
from app.services import onboard_jobs
from app.services.sub2api import sub2api_service
from app.services.settings import settings_service
from app.services.team import team_service
from app.services.vacancy import vacancy_service
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["seats"])


LIVE_FETCH_CONCURRENCY = 4


def group_local_children(
    cards: List[Dict[str, Any]],
    children: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    by_team: Dict[int, List[Dict[str, Any]]] = {}
    leftover: List[Dict[str, Any]] = []
    for child in children:
        raw = child.get("current_team_id") or child.get("last_team_id")
        try:
            team_id = int(raw) if raw not in (None, "") else None
        except (TypeError, ValueError):
            team_id = None
        if team_id:
            by_team.setdefault(team_id, []).append(child)
        else:
            leftover.append(child)
    groups: List[Dict[str, Any]] = []
    seen = set()
    for card in cards:
        team_id = int(card["id"])
        items = by_team.get(team_id) or []
        if not items:
            continue
        seen.add(team_id)
        groups.append({
            "team_id": team_id,
            "title": card.get("team_name") or card.get("email") or f"Team {team_id}",
            "children": items,
        })
    for team_id, items in by_team.items():
        if team_id in seen:
            continue
        groups.append({
            "team_id": team_id,
            "title": f"Team {team_id}",
            "children": items,
        })
    if leftover:
        groups.append({
            "team_id": None,
            "title": "未分配",
            "children": leftover,
        })
    return groups


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
    probe_rows = (await db.execute(select(ChildAccount))).scalars().all()
    probe_by_email = {
        str(child.email or "").lower(): child
        for child in probe_rows
        if child.email
    }
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
                stored = probe_by_email.get(email)
                probe_status = getattr(stored, "probe_status", None) or local.get("probe_status") or ""
                probe_label = getattr(stored, "probe_label", None) or local.get("probe_label") or ""
                probe_tone = {
                    "200": "ok",
                    "phone": "warn",
                    "429": "warn",
                    "401": "danger",
                    "403": "danger",
                }.get(probe_status, "muted")
                schedule_label = remote.get("schedule_label") or ""
                tone = remote.get("tone") or "muted"
                if probe_status in {"phone", "401", "403"} and probe_label:
                    schedule_label = probe_label
                    tone = probe_tone
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
                    "schedule_label": schedule_label,
                    "tone": tone,
                    "probe_status": probe_status,
                    "probe_label": probe_label,
                    "probe_tone": probe_tone,
                    "sub2api_account_id": remote.get("id") or local.get("sub2api_account_id") or getattr(stored, "sub2api_account_id", None),
                    "in_pool": bool(local or stored),
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
        await db.commit()
    sub2api_service.annotate_rotation(status.get("boxes") or [], cards)
    return cards, status


class OnboardRequest(BaseModel):
    team_id: int
    email: str = Field("", description="可空则自动领 HME；或 email----pickup_url；只填 iCloud 别名时走 Cloudflare 读码")
    phone: str = Field("", description="+1xxxx----https://api668.com/sms/by_key?key=...")
    proxy: str = Field("", description="子号静态 ISP，不填则用母号 ISP")
    password: str = ""
    reuse_existing: bool = True
    skip_invite: bool = False
    force: bool = False


class FreeOnboardRequest(BaseModel):
    email: str = Field("", description="可空则自动领 HME；或 email----pickup_url")
    phone: str = Field("", description="+1xxxx----https://api668.com/sms/by_key?key=...")
    proxy: str = Field("", description="子号静态 ISP，不填则用系统中心默认免费号代理")
    password: str = ""


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


class SeatOAuthStartRequest(BaseModel):
    team_id: int
    email: str
    origin: str = ""
    force_manual: bool = False
    phone: str = Field("", description="+1xxxx----https://api668.com/sms/by_key?key=...")


class SeatOAuthCompleteRequest(BaseModel):
    ticket: str
    callback_text: str = ""


def _public_base(request: Request, origin: str = "") -> str:
    raw = (origin or "").strip().rstrip("/")
    if raw.startswith("http://") or raw.startswith("https://"):
        return raw
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    proto = request.headers.get("x-forwarded-proto") or request.url.scheme
    if host:
        return f"{proto}://{host}".rstrip("/")
    return str(request.base_url).rstrip("/")


def _ps1_response(script: str, filename: str) -> PlainTextResponse:
    body = "\ufeff" + script.replace("\n", "\r\n")
    return PlainTextResponse(
        body,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )

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
    children = [child_account_service.serialize(item) for item in await child_account_service.list_accounts(db)]
    context.update({
        "cards": cards,
        "children": children,
        "child_groups": group_local_children(cards, children),
        "stats": await child_account_service.stats(db),
        "sub2api_status": sub2api_status,
        "free_account_proxy": await settings_service.get_setting(db, "free_account_proxy", ""),
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
    serialized = [child_account_service.serialize(item) for item in children]
    cards, sub2api_status = await load_sub2api_dashboard(db, allow_network=False)
    return {
        "success": True,
        "children": serialized,
        "child_groups": group_local_children(cards, serialized),
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


async def _complete_seat_oauth(db: AsyncSession, ticket: str, callback_text: str) -> Dict[str, Any]:
    from app.services import oauth_sessions
    from app.services.chatgpt import chatgpt_service
    from app.utils.jwt_parser import JWTParser

    session = oauth_sessions.consume_verifier(ticket)
    if not session:
        return {"success": False, "error": "认证会话不存在或已过期"}
    if session.get("status") == "done":
        return {"success": True, **oauth_sessions.public_session(session)}

    parsed = oauth_sessions.parse_oauth_callback(callback_text)
    code = parsed["code"]
    if not code:
        if not (callback_text or "").strip():
            return {"success": False, "error": "回调内容为空"}
        oauth_sessions.mark_session(ticket, status="error", error="回调里没有 code")
        return {"success": False, "error": "回调里没有 code"}
    if session.get("state") and parsed.get("state") and parsed.get("state") != session.get("state"):
        oauth_sessions.mark_session(ticket, status="error", error="state 不匹配")
        return {"success": False, "error": "state 不匹配，请重新点认证"}

    exchange = await chatgpt_service.exchange_oauth_code(
        code=code,
        client_id=session.get("client_id") or oauth_sessions.CLIENT_ID,
        redirect_uri=session.get("redirect_uri") or oauth_sessions.REDIRECT_URI,
        code_verifier=session.get("code_verifier") or "",
        db_session=db,
        identifier=f"oauth_{session.get('email') or 'seat'}",
    )
    if not exchange.get("success"):
        error = str(exchange.get("error") or "兑换 token 失败")
        oauth_sessions.mark_session(ticket, status="error", error=error)
        return {"success": False, "error": error}

    access_token = exchange.get("access_token") or ""
    id_token = exchange.get("id_token") or ""
    jwt = JWTParser()
    token_email = normalize_email(jwt.extract_email(access_token) or jwt.extract_email(id_token) or "")
    expected = normalize_email(session.get("email"))
    if token_email and expected and token_email != expected:
        error = f"登录的是 {token_email}，不是 {expected}"
        oauth_sessions.mark_session(ticket, status="error", error=error)
        return {"success": False, "error": error}

    email = expected or token_email
    team = await db.get(Team, int(session["team_id"]))
    if not team:
        return {"success": False, "error": "Team 不存在"}
    role = session.get("role") or ("owner" if normalize_email(team.email) == email else "child")
    if role == "owner":
        from app.services.encryption import encryption_service

        team.access_token_encrypted = encryption_service.encrypt_token(access_token)
        if exchange.get("refresh_token"):
            team.refresh_token_encrypted = encryption_service.encrypt_token(str(exchange["refresh_token"]))
        if id_token:
            team.id_token_encrypted = encryption_service.encrypt_token(id_token)
        team.client_id = session.get("client_id") or oauth_sessions.CLIENT_ID
        await db.flush()
        push_result = await sub2api_service.push_team(db, team)
        await db.commit()
        sync_result = await team_service.sync_team_info(team.id, db, force_refresh=False)
        probe = push_result.get("probe") or {}
        message = push_result.get("message") or f"{email} 已重新授权并推送到 Sub2API"
        if sync_result.get("success"):
            message += "，Team 成员信息已重新拉取"
        elif sync_result.get("error"):
            message += f"，但拉 Team 信息失败：{sync_result.get('error')}"
        result = {
            "success": True,
            "message": message,
            "email": email,
            "role": "owner",
            "account_id": push_result.get("account_id"),
            "probe": probe,
            "push": {
                "strategy": push_result.get("strategy") or "owner",
                "proxy_id": push_result.get("proxy_id"),
                "template": push_result.get("template"),
            },
        }
        oauth_sessions.mark_session(ticket, status="done", message=message, error="", result=result)
        return result

    child = await child_account_service.upsert_from_input(db, email=email)
    await child_account_service.save_tokens(db, child, {
        "access_token": access_token,
        "refresh_token": exchange.get("refresh_token") or "",
        "id_token": id_token,
        "client_id": session.get("client_id") or oauth_sessions.CLIENT_ID,
        "account_id": team.account_id or "",
    })
    if child.current_team_id is None:
        child.current_team_id = team.id
    push_result = await sub2api_service.import_session(
        db,
        email=email,
        access_token=access_token,
        refresh_token=exchange.get("refresh_token") or "",
        id_token=id_token,
        account_id=child.account_id or team.account_id or "",
        client_id=child.client_id or "",
        existing_id=child.sub2api_account_id,
        team=team,
        proxy_url=child.proxy or team.proxy or "",
        role="child",
    )
    if push_result.get("account_id"):
        child.sub2api_account_id = int(push_result["account_id"])
    await child_account_service.save_probe(db, child, push_result.get("probe"))
    await child_account_service.record_event(
        db,
        email=email,
        action="oauth",
        team_id=team.id,
        child_id=child.id,
        success=True,
        detail=f"probe={(push_result.get('probe') or {}).get('label') or '-'}",
    )
    await db.commit()
    probe = push_result.get("probe") or {}
    message = f"{email} 已重新授权并推送到 Sub2API"
    if probe.get("label"):
        message += f"，探测 {probe.get('label')}"
    result = {
        "success": True,
        "message": message,
        "email": email,
        "role": "child",
        "account_id": push_result.get("account_id"),
        "probe": probe,
        "push": {
            "strategy": push_result.get("strategy"),
            "proxy_id": push_result.get("proxy_id"),
            "template": push_result.get("template"),
        },
    }
    oauth_sessions.mark_session(ticket, status="done", message=message, error="", result=result)
    return result

async def _try_refresh_owner_team(db: AsyncSession, team: Team) -> Optional[Dict[str, Any]]:
    """母号 / Team 先用 RT/ST 换票。换成功就拉成员并推 Sub2API；换不了返回 None 走弹窗。"""
    sync = await team_service.sync_team_info(team.id, db, force_refresh=True)
    if sync.get("success"):
        push_result = await sub2api_service.push_team(db, team)
        await db.commit()
        message = sync.get("message") or "母号凭证仍可用，已重新拉到 Team 信息"
        if push_result.get("message"):
            message = f"{message}；{push_result['message']}"
        return {
            "success": True,
            "mode": "refreshed",
            "message": message,
            "email": team.email,
            "role": "owner",
            "account_id": push_result.get("account_id"),
            "probe": push_result.get("probe") or {},
            "push": {
                "strategy": push_result.get("strategy") or "owner",
                "proxy_id": push_result.get("proxy_id"),
                "template": push_result.get("template"),
            },
        }
    from app.services.reauth import owner_refresh_allows_oauth

    error_code = str(sync.get("error_code") or "")
    if owner_refresh_allows_oauth(error_code):
        return None
    return {
        "success": False,
        "error": sync.get("error") or "Team 换票失败",
        "error_code": error_code,
    }

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


async def _run_free_onboard_job(job_id: str, payload: FreeOnboardRequest) -> None:
    async with AsyncSessionLocal() as db:
        try:
            result = await onboard_service.register_free_account(
                db,
                email_line=payload.email,
                phone_line=payload.phone,
                proxy=payload.proxy,
                password=payload.password,
                job_id=job_id,
            )
            onboard_jobs.finish(job_id, result)
        except Exception as exc:
            logger.exception("后台免费号注册失败")
            onboard_jobs.finish(job_id, {"success": False, "error": str(exc), "error_code": "browser_failed"})
        finally:
            await db.close()


async def _run_auto_reauth_job(job_id: str, ticket: str) -> None:
    from app.services import oauth_sessions
    from app.services.browser_reauth import run_browser_oauth_reauth
    from app.services.mail_otp import parse_mail_line

    async with AsyncSessionLocal() as db:
        try:
            session = oauth_sessions.get_session(ticket)
            if not session:
                onboard_jobs.finish(job_id, {"success": False, "error": "认证会话不存在或已过期", "error_code": "oauth_expired"})
                return
            team = await db.get(Team, int(session["team_id"]))
            email = str(session.get("email") or "")
            child = await child_account_service.get_by_email(db, email)
            password = child_account_service.decrypt_secret(child.password_encrypted) if child else ""
            pickup_url = parse_mail_line(child.mail_raw).get("pickup_url") if child and child.mail_raw else ""
            cf_config = await onboard_service._cf_config(db)
            use_cloudflare = (not pickup_url) and bool(cf_config["admin_password"])
            phone = (child.phone if child else "") or ""
            sms_url = (child.sms_url if child else "") or ""
            proxy = ((child.proxy if child else "") or (team.proxy if team else "") or "")

            def on_stage(stage: str, message: str) -> None:
                onboard_jobs.note(job_id, stage, message)
                oauth_sessions.mark_session(ticket, status="running", message=message)

            onboard_jobs.note(job_id, "browser", "正在自动登录并完成授权")
            browser = await asyncio.to_thread(
                run_browser_oauth_reauth,
                email=email,
                password=password,
                authorize_url=str(session.get("authorize_url") or ""),
                proxy=proxy,
                pickup_url=pickup_url or "",
                phone=phone,
                sms_url=sms_url,
                use_cloudflare=use_cloudflare,
                cf_base_url=cf_config["base_url"],
                cf_address=cf_config["address"],
                cf_admin_password=cf_config["admin_password"],
                on_stage=on_stage,
            )
            if not browser.get("ok"):
                error = str(browser.get("error") or "自动授权失败")
                oauth_sessions.mark_session(ticket, status="error", error=error, message=error)
                onboard_jobs.finish(job_id, {
                    "success": False,
                    "error": error,
                    "error_code": browser.get("error_code") or "browser_failed",
                })
                return
            result = await _complete_seat_oauth(db, ticket, str(browser.get("callback_url") or ""))
            onboard_jobs.finish(job_id, result)
        except Exception as exc:
            logger.exception("自动重新授权失败")
            oauth_sessions.mark_session(ticket, status="error", error=str(exc), message=str(exc))
            onboard_jobs.finish(job_id, {"success": False, "error": str(exc), "error_code": "browser_failed"})
        finally:
            await db.close()


@router.post("/seats/oauth/start")
async def seats_oauth_start(
    payload: SeatOAuthStartRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services import oauth_sessions
    from app.services.chatgpt import chatgpt_service
    from app.services.mail_otp import parse_mail_line
    from app.services.reauth import auto_reauth_plan

    email = normalize_email(payload.email)
    team = await db.get(Team, payload.team_id)
    if not team:
        return JSONResponse(status_code=404, content={"success": False, "error": "Team 不存在"})
    if not email or "@" not in email:
        return JSONResponse(status_code=400, content={"success": False, "error": "邮箱不对"})
    role = "owner" if normalize_email(team.email) == email else "child"
    if role == "owner" and not payload.force_manual:
        refreshed = await _try_refresh_owner_team(db, team)
        if refreshed is not None:
            if refreshed.get("success"):
                return refreshed
            return JSONResponse(status_code=400, content=refreshed)
    child = None if role == "owner" else await child_account_service.get_by_email(db, email)
    if child is not None and str(payload.phone or "").strip():
        number, sms_url = parse_phone_line(payload.phone)
        if number:
            child.phone = number
        if sms_url:
            child.sms_url = sms_url
        child.updated_at = get_now()
        await db.commit()
        await db.refresh(child)
    password = child_account_service.decrypt_secret(child.password_encrypted) if child else ""
    pickup_url = parse_mail_line(child.mail_raw).get("pickup_url") if child and child.mail_raw else ""
    cf_config = await onboard_service._cf_config(db)
    use_cloudflare = (not pickup_url) and bool(cf_config["admin_password"])
    proxy = ((child.proxy if child else "") or team.proxy or "").strip()
    plan = {"auto": False, "reason": "已改走手动授权"} if payload.force_manual else auto_reauth_plan(
        email=email,
        role=role,
        password=password,
        pickup_url=pickup_url or "",
        cf_ready=use_cloudflare,
        proxy=proxy,
    )
    auth = chatgpt_service.create_oauth_authorize_url(
        client_id=oauth_sessions.CLIENT_ID,
        redirect_uri=oauth_sessions.REDIRECT_URI,
        login_hint=email,
    )
    auth["client_id"] = oauth_sessions.CLIENT_ID
    session = oauth_sessions.create_session(
        team_id=team.id,
        email=email,
        authorize=auth,
        role=role,
        mode="auto" if plan["auto"] else "manual",
        proxy=proxy,
        password=password,
        team_name=team.team_name or "",
    )
    base = _public_base(request, payload.origin)
    complete_url = f"{base}/admin/seats/oauth/complete"
    launcher_url = f"{base}/admin/seats/oauth/{session['ticket']}/launcher.ps1"
    proto_url = oauth_sessions.protocol_url(session["ticket"], base)
    install_url = f"{base}/admin/seats/oauth/install.ps1"
    if plan["auto"]:
        active = onboard_jobs.active_job_for_email(email)
        if active:
            if str(payload.phone or "").strip():
                onboard_jobs.finish(active["id"], {
                    "success": False,
                    "error": "已换号重试",
                    "error_code": "cancelled",
                    "status": "cancelled",
                })
            else:
                return {
                    "success": True,
                    "mode": "auto",
                    "job_id": active["id"],
                    "session": session,
                    "complete_url": complete_url,
                    "launcher_url": launcher_url,
                    "protocol_url": proto_url,
                    "install_url": install_url,
                    "message": "该邮箱已有进行中的任务",
                    "job": active,
                }
        job = onboard_jobs.create_job(team_id=team.id, email=email, action="reauth")
        oauth_sessions.mark_session(session["ticket"], job_id=job["id"], status="running", message=plan["reason"])
        live = oauth_sessions.get_session(session["ticket"]) or {}
        asyncio.create_task(_run_auto_reauth_job(job["id"], session["ticket"]))
        return {
            "success": True,
            "mode": "auto",
            "job_id": job["id"],
            "session": oauth_sessions.public_session(live),
            "complete_url": complete_url,
            "launcher_url": launcher_url,
            "protocol_url": proto_url,
            "install_url": install_url,
            "message": plan["reason"],
            "job": job,
        }
    return {
        "success": True,
        "mode": "manual",
        "session": session,
        "complete_url": complete_url,
        "launcher_url": launcher_url,
        "protocol_url": proto_url,
        "install_url": install_url,
        "message": "等待本机授权窗口",
    }


@router.get("/seats/oauth/install.ps1")
async def seats_oauth_install(current_user: dict = Depends(require_admin)):
    from app.services import oauth_sessions

    return _ps1_response(oauth_sessions.install_protocol_script(), "install-team48-oauth.ps1")


@router.get("/seats/oauth/{ticket}/launch.json")
async def seats_oauth_launch_json(ticket: str, request: Request):
    from app.services import oauth_sessions

    session = oauth_sessions.get_session(ticket)
    if not session:
        return JSONResponse(status_code=404, content={"success": False, "error": "认证会话不存在或已过期"})
    complete_url = f"{_public_base(request)}/admin/seats/oauth/complete"
    return oauth_sessions.launch_payload(session, complete_url)


@router.post("/seats/oauth/{ticket}/ack")
async def seats_oauth_ack(ticket: str):
    from app.services import oauth_sessions

    session = oauth_sessions.mark_session(ticket, message="本机授权窗口已打开")
    if not session:
        return JSONResponse(status_code=404, content={"success": False, "error": "认证会话不存在或已过期"})
    return {"success": True, "session": oauth_sessions.public_session(session)}

@router.get("/seats/oauth/{ticket}")
async def seats_oauth_status(
    ticket: str,
    current_user: dict = Depends(require_admin),
):
    from app.services import oauth_sessions

    session = oauth_sessions.get_session(ticket)
    if not session:
        return JSONResponse(status_code=404, content={"success": False, "error": "认证会话不存在或已过期"})
    return {"success": True, "session": oauth_sessions.public_session(session)}


@router.get("/seats/oauth/{ticket}/launcher.ps1")
async def seats_oauth_launcher(
    ticket: str,
    request: Request,
    current_user: dict = Depends(require_admin),
):
    from app.services import oauth_sessions

    session = oauth_sessions.get_session(ticket)
    if not session:
        return JSONResponse(status_code=404, content={"success": False, "error": "认证会话不存在或已过期"})
    complete_url = f"{_public_base(request)}/admin/seats/oauth/complete"
    script = oauth_sessions.launcher_script(session, complete_url)
    return PlainTextResponse(
        script,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="team48-oauth-{ticket[:8]}.ps1"'},
    )


@router.post("/seats/oauth/complete")
async def seats_oauth_complete(
    payload: SeatOAuthCompleteRequest,
    db: AsyncSession = Depends(get_db),
):
    result = await _complete_seat_oauth(db, payload.ticket, payload.callback_text)
    status_code = 200 if result.get("success") else 400
    return JSONResponse(status_code=status_code, content=result)




@router.get("/seats/hme-status")
async def seats_hme_status(
    db: AsyncSession = Depends(get_db),
    current_user: dict = Depends(require_admin),
):
    from app.services.hme import probe_status

    status = await probe_status(db)
    return {"success": bool(status.get("ok")), **status}


@router.post("/seats/onboard")
async def seats_onboard(
    payload: OnboardRequest,
    current_user: dict = Depends(require_admin),
):
    email = normalize_email(payload.email.split("----", 1)[0] if payload.email else "")
    if email:
        active = onboard_jobs.active_job_for_email(email)
        if active:
            return {"success": True, "accepted": True, "job_id": active["id"], "message": "该邮箱已有进行中的拉人任务", "job": active}
    job = onboard_jobs.create_job(team_id=payload.team_id, email=email or payload.email, action="onboard")
    asyncio.create_task(_run_onboard_job(job["id"], payload))
    return {"success": True, "accepted": True, "job_id": job["id"], "message": "已开始拉人，进度会留在本页", "job": job}


@router.post("/seats/onboard-free")
async def seats_onboard_free(
    payload: FreeOnboardRequest,
    current_user: dict = Depends(require_admin),
):
    email = normalize_email(payload.email.split("----", 1)[0] if payload.email else "")
    if email:
        active = onboard_jobs.active_job_for_email(email)
        if active:
            return {"success": True, "accepted": True, "job_id": active["id"], "message": "该邮箱已有进行中的任务", "job": active}
    job = onboard_jobs.create_job(team_id=0, email=email or payload.email, action="free_register")
    asyncio.create_task(_run_free_onboard_job(job["id"], payload))
    return {"success": True, "accepted": True, "job_id": job["id"], "message": "已开始注册免费号，进度会留在本页", "job": job}


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
    if child.status == "free" or not team_id:
        if child.status in {"active", "invited"} and not team_id:
            return JSONResponse(status_code=400, content={"success": False, "error": "这个子号没有关联 Team，请先在表单里选 Team 再拉"})
        active = onboard_jobs.active_job_for_email(child.email)
        if active:
            return {"success": True, "accepted": True, "job_id": active["id"], "message": "该邮箱已有进行中的任务", "job": active}
        request = FreeOnboardRequest(
            email=payload.email or child.mail_raw or child.email,
            phone=payload.phone or ((child.phone or "") + ("----" + child.sms_url if child.sms_url else "")),
            proxy=payload.proxy or child.proxy or "",
        )
        job = onboard_jobs.create_job(team_id=0, email=child.email, action="free_register")
        asyncio.create_task(_run_free_onboard_job(job["id"], request))
        return {"success": True, "accepted": True, "job_id": job["id"], "message": f"开始注册免费号 {child.email}", "job": job}
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
