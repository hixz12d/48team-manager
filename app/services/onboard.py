"""子号拉人引擎：邀请确认 -> 默认登录/授权链接注册 -> 接码 -> 对账 -> Codex 授权换 RT -> 推 Sub2API。"""
from __future__ import annotations

import asyncio
import logging
import secrets
import string
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import ChildAccount, Team, TeamEmailMapping
from app.services.child_accounts import (
    ACTIVE_CHILD_STATUSES,
    CHILD_STATUS_ACTIVE,
    CHILD_STATUS_INVITED,
    CHILD_STATUS_STANDBY,
    CHILD_STATUS_FREE,
    OWNER_ROLES,
    child_account_service,
    coerce_local_datetime,
    is_workspace_account_id,
    normalize_email,
)
from app.services.mail_otp import parse_mail_line
from app.services import oauth_sessions, onboard_jobs
from app.services.sms import parse_phone_line, require_proxy
from app.services.sub2api import sub2api_service
from app.services.team import team_service
from app.services.chatgpt import ChatGPTService, chatgpt_service
from app.services.team_view import present_occupancy
from app.services.vacancy import chatgpt_member_ids, is_safe_to_refill, summarize_for_message
from app.utils.proxy import normalize_proxy_url
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

KICK_COOLDOWN_SECONDS = 10 * 60
MEMBER_LOOKUP_RETRIES = 3
MEMBER_LOOKUP_INTERVAL = 3.0


def _random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12)) + "Aa1!"


def should_wait_for_invite_mail(already_invited: bool) -> bool:
    return False


def classify_onboard_error(error: str, *, stage: str = "") -> str:
    text = (error or "").lower()
    if "cancelled" in text or "已取消" in error:
        return "cancelled"
    if (
        "account_deactivated" in text
        or "has been deactivated" in text
        or "is deactivated" in text
        or "deactivated_workspace" in text
        or "已被 deactivate" in error
    ):
        return "account_deactivated"
    if "coroutine" in text:
        return "browser_await_bug"
    if "too many" in text or "限流" in error or "max_check" in text:
        return "openai_rate_limited"
    if "仍未通过" in error or "mail_otp_rejected" in text:
        return "mail_otp_rejected"
    if "sms_rejected" in text or "不被 openai 接受" in text or "停在美国" in error or "绑满" in error:
        return "sms_rejected"
    if "号码池" in error or "phone_pool" in text:
        return "phone_pool_empty"
    if "sms_missing" in text or "sms_failed" in text or "接码" in error or "手机号页" in error or "sms" in text:
        return "sms_failed"
    if "otp" in text or "mailbox" in text or "验证码" in error:
        return "mail_otp_timeout"
    if "邮箱页" in error or "email_gate" in text or "email_input_missing" in text:
        return "email_gate_stuck"
    if "cloudflare" in text or "just a moment" in text or "turnstile" in text:
        return "cloudflare_challenge"
    if "代理" in error or "proxy" in text:
        return "proxy_failed"
    if "已满" in error or "full" in text:
        return "team_full"
    if "降级" in error or "degraded" in text:
        return "master_degraded"
    if "封禁" in error or "banned" in text:
        return "master_banned"
    if "过期" in error or "expired" in text:
        return "master_expired"
    if "冷却" in error or "cooldown" in text:
        return "kick_cooldown"
    if stage == "push" or "sub2api" in text:
        return "push_failed"
    if stage == "reconcile" or "对账" in error or "未看到该成员" in error:
        return "not_joined"
    if stage == "invite":
        return "invite_failed"
    if "refresh_token" in text or "没有 refresh" in error:
        return "oauth_no_refresh"
    if "登录的是" in error:
        return "oauth_identity_mismatch"
    if stage == "oauth" or "无法自动授权" in error or "自动授权" in error:
        return "oauth_failed"
    return "browser_failed"


class OnboardService:
    async def _load_team(self, db_session: AsyncSession, team_id: int) -> Team:
        stmt = select(Team).where(Team.id == team_id).options(selectinload(Team.email_mappings))
        result = await db_session.execute(stmt)
        team = result.scalar_one_or_none()
        if not team:
            raise ValueError(f"Team {team_id} 不存在")
        return team

    def _child_proxy(self, child: ChildAccount, team: Optional[Team] = None) -> str:
        return require_proxy(child.proxy or (team.proxy if team else ""), "子号浏览器/接码")

    def _done_verb(self, team: Optional[Team]) -> str:
        return "已入组" if team is not None else "已注册"

    def _team_proxy(self, team: Team) -> str:
        return require_proxy(team.proxy, "母号 Team API")

    async def _mapping(self, db_session: AsyncSession, team_id: int, email: str) -> Optional[TeamEmailMapping]:
        result = await db_session.execute(
            select(TeamEmailMapping).where(
                TeamEmailMapping.team_id == team_id,
                TeamEmailMapping.email == normalize_email(email),
            )
        )
        return result.scalar_one_or_none()

    async def _confirm_joined(self, db_session: AsyncSession, team_id: int, email: str) -> bool:
        members = await team_service.get_team_members(team_id, db_session)
        if not members.get("success"):
            raise RuntimeError(members.get("error") or "对账成员列表失败")
        target = normalize_email(email)
        for item in members.get("members") or []:
            if normalize_email(item.get("email")) == target and item.get("status") == "joined":
                return True
        return False

    async def _lookup_live_member(
        self,
        db_session: AsyncSession,
        team_id: int,
        email: str,
        *,
        retries: int = MEMBER_LOOKUP_RETRIES,
        interval: float = MEMBER_LOOKUP_INTERVAL,
    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        target = normalize_email(email)
        last: Dict[str, Any] = {"success": False, "members": [], "error": "未查询成员"}
        attempts = max(1, int(retries))
        for attempt in range(attempts):
            last = await team_service.get_team_members(team_id, db_session)
            if last.get("success"):
                for item in last.get("members") or []:
                    if normalize_email(item.get("email")) == target:
                        return last, item
            elif attempt < attempts - 1:
                await asyncio.sleep(interval)
                continue
            if attempt < attempts - 1:
                logger.info("成员列表暂无 %s，%.1fs 后重试 (%s/%s)", email, interval, attempt + 1, attempts - 1)
                await asyncio.sleep(interval)
        return last, None

    async def _cf_config(self, db_session: AsyncSession) -> Dict[str, str]:
        from app.services.cloudflare_mail import (
            CF_SETTING_ADDRESS,
            CF_SETTING_ADMIN_PASSWORD,
            CF_SETTING_BASE_URL,
            DEFAULT_CF_MAIL_ADDRESS,
            DEFAULT_CF_MAIL_BASE_URL,
        )
        from app.services.settings import settings_service

        return {
            "base_url": (await settings_service.get_setting(db_session, CF_SETTING_BASE_URL, DEFAULT_CF_MAIL_BASE_URL) or DEFAULT_CF_MAIL_BASE_URL).strip(),
            "address": (await settings_service.get_setting(db_session, CF_SETTING_ADDRESS, DEFAULT_CF_MAIL_ADDRESS) or DEFAULT_CF_MAIL_ADDRESS).strip(),
            "admin_password": (await settings_service.get_setting(db_session, CF_SETTING_ADMIN_PASSWORD, "") or "").strip(),
        }

    def _run_browser(self, **kwargs: Any) -> Dict[str, Any]:
        from app.services.browser_onboard import run_browser_onboard

        return run_browser_onboard(**kwargs)

    def _run_oauth_browser(self, **kwargs: Any) -> Dict[str, Any]:
        from app.services.browser_reauth import run_browser_oauth_reauth

        return run_browser_oauth_reauth(**kwargs)

    def _bind_phone(self, phone_line: str, job_id: Optional[str]):
        manual = str(phone_line or "").strip()
        if manual:
            number, sms_url = parse_phone_line(manual)
            if job_id and number:
                onboard_jobs.update_phone(job_id, number)
            return number, sms_url, None
        if not job_id:
            return "", "", None
        from app.services.phone_pool import phone_pool_service

        def on_log(stage: str, message: str) -> None:
            onboard_jobs.note(job_id, stage, message)
            text = str(message or "")
            if stage == "add_phone" and text.startswith("领取 "):
                onboard_jobs.update_phone(job_id, text[3:].strip())

        return "", "", phone_pool_service.make_sync_source(
            job_id,
            on_log=on_log,
        )

    @staticmethod
    def _apply_browser_phone(
        child: ChildAccount,
        browser_result: Dict[str, Any],
        fallback_phone: str = "",
        fallback_sms: str = "",
    ) -> None:
        number = str(browser_result.get("phone") or fallback_phone or "").strip()
        sms_url = str(browser_result.get("sms_url") or fallback_sms or "").strip()
        if number:
            child.phone = number
        if sms_url:
            child.sms_url = sms_url

    def _cancelled(self, job_id: Optional[str]) -> bool:
        return onboard_jobs.is_cancelled(job_id)

    async def _progress(
        self,
        db_session: AsyncSession,
        child: Optional[ChildAccount],
        *,
        job_id: Optional[str],
        stage: str,
        message: str,
        error: str = "",
        error_code: str = "",
    ) -> None:
        onboard_jobs.note(job_id, stage, message, error=error, error_code=error_code)
        if child is not None:
            await child_account_service.set_progress(
                db_session,
                child,
                stage=stage,
                job_id=job_id,
                error=error or None,
                clear_error=not error,
            )
            await db_session.commit()

    def _master_block_reason(self, team: Team) -> Optional[str]:
        if team.status == "banned":
            return "母号已封禁，停止拉人"
        if team.status == "expired":
            return "母号已过期，停止拉人"
        role = (getattr(team, "account_role", None) or "").strip()
        if role and role not in OWNER_ROLES:
            return f"母号角色已降级为 {role}，停止拉人"
        return None

    async def preview_reconcile(self, db_session: AsyncSession, team_id: int) -> Dict[str, Any]:
        team = await self._load_team(db_session, team_id)
        live = await team_service.get_team_members(team_id, db_session)
        if not live.get("success"):
            return {"success": False, "error": live.get("error") or "读取上游成员失败", "findings": []}

        children = await child_account_service.list_accounts(db_session)
        local_by_email = {
            item.email: item
            for item in children
            if item.current_team_id == team.id or item.last_team_id == team.id
        }
        findings = []
        owner = normalize_email(team.email)
        live_emails = set()
        for member in live.get("members") or []:
            email = normalize_email(member.get("email"))
            if not email or email == owner:
                continue
            live_emails.add(email)
            child = local_by_email.get(email)
            if child is None:
                findings.append({
                    "type": "ghost",
                    "email": email,
                    "live_status": member.get("status"),
                    "detail": "上游有、本地子号库没有",
                })
            elif child.status == CHILD_STATUS_STANDBY:
                findings.append({
                    "type": "misaligned",
                    "email": email,
                    "live_status": member.get("status"),
                    "local_status": child.status,
                    "detail": "上游还在，本地已标 standby",
                })
            elif child.status == "unused" and member.get("status") == "invited":
                findings.append({
                    "type": "leftover_invite",
                    "email": email,
                    "live_status": member.get("status"),
                    "local_status": child.status,
                    "detail": "上游邀请还在，本地已回到未使用",
                })
            elif child.status == CHILD_STATUS_INVITED and member.get("status") != "invited":
                findings.append({
                    "type": "joined_unmarked",
                    "email": email,
                    "live_status": member.get("status"),
                    "local_status": child.status,
                    "joined_at": member.get("added_at") or member.get("joined_at"),
                    "detail": "上游已加入，本地还停在已邀请未注册",
                })
        for child in children:
            if child.current_team_id != team.id:
                continue
            if child.email not in live_emails and child.status in ACTIVE_CHILD_STATUSES:
                findings.append({
                    "type": "leftover_local",
                    "email": child.email,
                    "live_status": None,
                    "local_status": child.status,
                    "detail": "本地还占着这个 Team，上游已经看不到",
                })
        occupancy = present_occupancy({
            "current_members": team.current_members,
            "max_members": team.max_members,
            "live_members": live.get("members") or [],
        })
        return {
            "success": True,
            "team_id": team.id,
            "findings": findings,
            "occupancy": occupancy,
            "dry_run": True,
        }

    async def _team_by_id(self, db_session: AsyncSession, team_id: Optional[int]) -> Optional[Team]:
        if not team_id:
            return None
        try:
            return await self._load_team(db_session, int(team_id))
        except Exception:
            return None

    async def resolve_workspace_account_id(
        self,
        db_session: AsyncSession,
        child: ChildAccount,
        team: Optional[Team] = None,
    ) -> str:
        candidates = [team] if team else []
        for team_id in (child.current_team_id, child.last_team_id):
            if team and team_id == team.id:
                continue
            found = await self._team_by_id(db_session, team_id)
            if found:
                candidates.append(found)
        for item in candidates:
            if item and is_workspace_account_id(item.account_id):
                return str(item.account_id).strip()
        if is_workspace_account_id(child.account_id):
            return str(child.account_id).strip()
        return ""

    async def fix_child_account_id(
        self,
        db_session: AsyncSession,
        *,
        child_id: Optional[int] = None,
        email: str = "",
        team_id: Optional[int] = None,
        push: bool = True,
    ) -> Dict[str, Any]:
        child = None
        if child_id:
            child = await child_account_service.get_by_id(db_session, child_id)
        elif email:
            child = await child_account_service.get_by_email(db_session, email)
        if not child:
            return {"success": False, "error": "子号不存在"}

        team = await self._team_by_id(db_session, team_id or child.current_team_id or child.last_team_id)
        workspace = await self.resolve_workspace_account_id(db_session, child, team)
        if not workspace:
            return {"success": False, "error": "找不到可用的 workspace account_id，先把母号 account_id 修成 UUID"}

        old = (child.account_id or "").strip()
        child.account_id = workspace
        push_result = None
        if push:
            access_token = child_account_service.decrypt_secret(child.access_token_encrypted)
            if not access_token:
                await child_account_service.record_event(
                    db_session,
                    email=child.email,
                    action="fix_account_id",
                    team_id=team.id if team else None,
                    child_id=child.id,
                    success=True,
                    detail=f"{old or '-'} -> {workspace}，无 token 未回推",
                )
                await db_session.commit()
                return {
                    "success": True,
                    "message": f"已把 {child.email} 的 account_id 改成 {workspace}，但没有 access token，未回推 Sub2API",
                    "old_account_id": old,
                    "account_id": workspace,
                    "pushed": False,
                    "child": child_account_service.serialize(child),
                }
            try:
                push_result = await sub2api_service.import_session(
                    db_session,
                    email=child.email,
                    access_token=access_token,
                    refresh_token=child_account_service.decrypt_secret(child.refresh_token_encrypted),
                    id_token=child_account_service.decrypt_secret(child.id_token_encrypted),
                    account_id=workspace,
                    client_id=child.client_id or "",
                    existing_id=child.sub2api_account_id,
                    team=team,
                    proxy_url=child.proxy or (team.proxy if team else "") or "",
                    role="child",
                )
                if push_result.get("account_id"):
                    child.sub2api_account_id = int(push_result["account_id"])
                await child_account_service.save_probe(db_session, child, push_result.get("probe"))
            except Exception as exc:  # noqa: BLE001
                await child_account_service.record_event(
                    db_session,
                    email=child.email,
                    action="fix_account_id",
                    team_id=team.id if team else None,
                    child_id=child.id,
                    success=False,
                    detail=str(exc),
                    error_code="push_failed",
                )
                await db_session.commit()
                return {
                    "success": False,
                    "error": f"account_id 已改成 {workspace}，但回推 Sub2API 失败: {exc}",
                    "old_account_id": old,
                    "account_id": workspace,
                    "child": child_account_service.serialize(child),
                }

        await child_account_service.record_event(
            db_session,
            email=child.email,
            action="fix_account_id",
            team_id=team.id if team else None,
            child_id=child.id,
            success=True,
            detail=f"{old or '-'} -> {workspace}",
        )
        await db_session.commit()
        message = f"已把 {child.email} 的 account_id 改成 {workspace}"
        if push_result:
            message += "，并已回推 Sub2API"
        return {
            "success": True,
            "message": message,
            "old_account_id": old,
            "account_id": workspace,
            "pushed": bool(push_result),
            "push": push_result,
            "child": child_account_service.serialize(child),
        }

    async def apply_reconcile(self, db_session: AsyncSession, team_id: int) -> Dict[str, Any]:
        preview = await self.preview_reconcile(db_session, team_id)
        if not preview.get("success"):
            return preview

        applied: list[Dict[str, Any]] = []
        skipped: list[Dict[str, Any]] = []
        for finding in preview.get("findings") or []:
            kind = finding.get("type")
            email = finding.get("email") or ""
            if kind == "leftover_invite":
                result = await self.kick_to_standby(db_session, team_id=team_id, email=email)
                applied.append({**finding, "action": "revoke_invite", "result": result.get("message") or result.get("error")})
                continue
            if kind == "joined_unmarked":
                child = await child_account_service.get_by_email(db_session, email)
                if not child:
                    skipped.append({**finding, "reason": "本地子号已不在"})
                    continue
                mapping = await self._mapping(db_session, team_id, email)
                await child_account_service.mark_active(
                    db_session,
                    child,
                    await self._load_team(db_session, team_id),
                    mapping=mapping,
                    joined_at=coerce_local_datetime(finding.get("joined_at")),
                )
                await child_account_service.record_event(
                    db_session,
                    email=email,
                    action="reconcile",
                    team_id=team_id,
                    child_id=child.id,
                    success=True,
                    detail="mark_active",
                )
                applied.append({**finding, "action": "mark_active", "result": "已改成在席"})
                continue
            if kind == "leftover_local":
                child = await child_account_service.get_by_email(db_session, email)
                if not child:
                    skipped.append({**finding, "reason": "本地子号已不在"})
                    continue
                mapping = await self._mapping(db_session, team_id, email)
                if child.status == CHILD_STATUS_INVITED:
                    await child_account_service.mark_unused(db_session, child, mapping=mapping, stage="reconcile")
                    action = "mark_unused"
                else:
                    await child_account_service.mark_standby(db_session, child, mapping=mapping)
                    action = "mark_standby"
                await child_account_service.record_event(
                    db_session,
                    email=email,
                    action="reconcile",
                    team_id=team_id,
                    child_id=child.id,
                    success=True,
                    detail=action,
                )
                applied.append({**finding, "action": action, "result": "已纠正本地状态"})
                continue
            skipped.append({**finding, "reason": "需人工确认，未自动踢上游"})

        await db_session.commit()
        message = f"已修 {len(applied)} 条本地/邀请错位"
        if skipped:
            message += f"，另有 {len(skipped)} 条需人工确认"
        return {
            "success": True,
            "message": message,
            "applied": applied,
            "skipped": skipped,
            "findings": preview.get("findings") or [],
            "occupancy": preview.get("occupancy"),
            "dry_run": False,
        }


    async def invite_and_onboard(
        self,
        db_session: AsyncSession,
        *,
        team_id: int,
        email_line: str,
        phone_line: str = "",
        proxy: str = "",
        password: str = "",
        reuse_existing: bool = True,
        skip_invite: bool = False,
        force: bool = False,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        from app.services import hme as hme_service

        claimed = None
        try:
            email_line, claimed = await hme_service.maybe_claim_alias(
                db_session,
                email_line,
                job_id=job_id or "",
                purpose="onboard",
                team_id=team_id,
            )
            if claimed:
                onboard_jobs.update_email(job_id, claimed.email)
                onboard_jobs.note(job_id, "hme", f"已领取 HME 别名 {claimed.email}")
            result = await self._invite_and_onboard_impl(
                db_session,
                team_id=team_id,
                email_line=email_line,
                phone_line=phone_line,
                proxy=proxy,
                password=password,
                reuse_existing=reuse_existing,
                skip_invite=skip_invite,
                force=force,
                job_id=job_id,
            )
            label = ""
            if claimed and result.get("success"):
                team = await self._load_team(db_session, team_id)
                cfg = await hme_service.load_config(db_session)
                label = hme_service.resolve_team_tag(team, cfg.team_tag_map)
            await hme_service.finalize_claim(db_session, claimed, result, label)
            return result
        except hme_service.HmeError as exc:
            await hme_service.finalize_claim(db_session, claimed, {"success": False})
            if job_id:
                onboard_jobs.note(job_id, "hme_failed", str(exc), error=str(exc), error_code=exc.code)
            return {"success": False, "error": str(exc), "error_code": exc.code, "status": "hme_failed"}
        except Exception:
            await hme_service.finalize_claim(db_session, claimed, {"success": False})
            raise

    async def register_free_account(
        self,
        db_session: AsyncSession,
        *,
        email_line: str,
        phone_line: str = "",
        proxy: str = "",
        password: str = "",
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        from app.services import hme as hme_service

        claimed = None
        try:
            email_line, claimed = await hme_service.maybe_claim_alias(
                db_session,
                email_line,
                job_id=job_id or "",
                purpose="free",
            )
            if claimed:
                onboard_jobs.update_email(job_id, claimed.email)
                onboard_jobs.note(job_id, "hme", f"已领取 HME 别名 {claimed.email}")
            result = await self._register_free_account_impl(
                db_session,
                email_line=email_line,
                phone_line=phone_line,
                proxy=proxy,
                password=password,
                job_id=job_id,
            )
            label = hme_service.FREE_ACCOUNT_LABEL if claimed and result.get("success") else ""
            await hme_service.finalize_claim(db_session, claimed, result, label)
            return result
        except hme_service.HmeError as exc:
            await hme_service.finalize_claim(db_session, claimed, {"success": False})
            if job_id:
                onboard_jobs.note(job_id, "hme_failed", str(exc), error=str(exc), error_code=exc.code)
            return {"success": False, "error": str(exc), "error_code": exc.code, "status": "hme_failed"}
        except Exception:
            await hme_service.finalize_claim(db_session, claimed, {"success": False})
            raise

    async def _invite_and_onboard_impl(
        self,
        db_session: AsyncSession,
        *,
        team_id: int,
        email_line: str,
        phone_line: str = "",
        proxy: str = "",
        password: str = "",
        reuse_existing: bool = True,
        skip_invite: bool = False,
        force: bool = False,
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        parsed = parse_mail_line(email_line)
        email = normalize_email(parsed["email"] or email_line)
        if not email or "@" not in email:
            raise ValueError("请输入有效邮箱")

        team = await self._load_team(db_session, team_id)
        self._team_proxy(team)
        child_proxy = proxy or ""
        existing = await child_account_service.get_by_email(db_session, email)
        if (
            existing
            and existing.status == CHILD_STATUS_ACTIVE
            and existing.current_team_id == team.id
            and child_account_service.decrypt_secret(existing.refresh_token_encrypted)
        ):
            return {"success": True, "status": "already_exists", "message": f"{email} 已在该 Team 中", "child": child_account_service.serialize(existing)}

        block = self._master_block_reason(team)
        if block:
            return {"success": False, "error": block, "error_code": classify_onboard_error(block), "status": "blocked"}

        if existing and existing.kicked_at and existing.status == CHILD_STATUS_STANDBY and not force:
            elapsed = (get_now() - existing.kicked_at).total_seconds()
            if elapsed < KICK_COOLDOWN_SECONDS:
                remain = int((KICK_COOLDOWN_SECONDS - elapsed) / 60) + 1
                error = f"{email} 刚被踢出，{remain} 分钟内不要再拉进任何 Team，避免 token_revoked"
                return {"success": False, "error": error, "error_code": "kick_cooldown", "status": "blocked"}

        if self._cancelled(job_id):
            return {"success": False, "error": "已取消", "error_code": "cancelled", "status": "cancelled"}

        phone, sms_url, phone_source = self._bind_phone(phone_line, job_id)
        if existing:
            if str(phone_line or "").strip():
                phone = phone or existing.phone or ""
                sms_url = sms_url or existing.sms_url or ""
            child_proxy = child_proxy or existing.proxy or ""
            password = password or child_account_service.decrypt_secret(existing.password_encrypted)
        child_proxy = child_proxy or team.proxy or ""
        if child_proxy:
            child_proxy = normalize_proxy_url(child_proxy) or child_proxy

        child = await child_account_service.upsert_from_input(
            db_session,
            email=email,
            password=password,
            mail_raw=parsed.get("raw") or email_line,
            phone=phone,
            sms_url=sms_url,
            proxy=child_proxy,
            cycle_days=team.seat_cycle_days or 7,
        )
        password = password or child_account_service.decrypt_secret(child.password_encrypted)
        await self._progress(db_session, child, job_id=job_id, stage="checking", message="正在检查母号和占用")

        live, live_item = await self._lookup_live_member(db_session, team.id, email, retries=1)
        occupancy = present_occupancy({
            "current_members": team.current_members,
            "max_members": team.max_members,
            "live_members": (live.get("members") if live.get("success") else None),
            "live_error": None if live.get("success") else live.get("error"),
        })
        already_invited = bool(live_item and live_item.get("status") == "invited")
        already_joined = bool(live_item and live_item.get("status") == "joined")
        if already_joined:
            await child_account_service.mark_active(db_session, child, team, mapping=await self._mapping(db_session, team.id, email))
            await db_session.commit()
            if child_account_service.decrypt_secret(child.refresh_token_encrypted):
                return {"success": True, "status": "already_exists", "message": f"{email} 已在该 Team 中", "child": child_account_service.serialize(child)}
            return await self._finish_callable_child(db_session, child, team, email=email, job_id=job_id, phone_line=phone_line)

        if not password:
            password = _random_password()
            child.password_encrypted = child_account_service.encrypt_secret(password)

        if not skip_invite and not already_invited:
            capacity = occupancy.get("capacity")
            occupied = occupancy.get("upstream_occupied")
            if occupied is None:
                occupied = occupancy.get("occupied")
            if capacity is not None and occupied is not None and occupied >= capacity:
                error = f"占用 {occupied}/{capacity}，不能再邀请"
                await self._progress(db_session, child, job_id=job_id, stage="blocked", message=error, error=error, error_code="team_full")
                return {"success": False, "error": error, "error_code": "team_full", "status": "blocked", "occupancy": occupancy}

        if self._cancelled(job_id):
            return {"success": False, "error": "已取消", "error_code": "cancelled", "status": "cancelled"}

        if already_invited:
            await child_account_service.mark_invited(db_session, child, team)
            await child_account_service.record_event(
                db_session,
                email=email,
                action="reregister" if skip_invite else "invite",
                team_id=team.id,
                child_id=child.id,
                success=True,
                detail="复用已发出的邀请" if already_invited else "跳过重复邀请",
            )
            await self._progress(db_session, child, job_id=job_id, stage="invited", message="邀请已存在，开始重新注册")
        else:
            await self._progress(db_session, child, job_id=job_id, stage="inviting", message="正在发送 Team 邀请")
            invite = await team_service.add_team_member(team_id, email, db_session)
            if not invite.get("success"):
                error = invite.get("error") or "邀请失败"
                code = classify_onboard_error(error, stage="invite")
                await child_account_service.record_event(
                    db_session,
                    email=email,
                    action="invite",
                    team_id=team.id,
                    child_id=child.id,
                    success=False,
                    detail=error,
                    error_code=code,
                )
                await self._progress(db_session, child, job_id=job_id, stage="invite_failed", message=error, error=error, error_code=code)
                return {"success": False, "error": error, "error_code": code, "status": "invite_failed"}

            await child_account_service.mark_invited(db_session, child, team)
            await child_account_service.record_event(
                db_session,
                email=email,
                action="invite",
                team_id=team.id,
                child_id=child.id,
                success=True,
                detail=invite.get("message") or "邀请已发送",
            )
            await self._progress(db_session, child, job_id=job_id, stage="invited", message="邀请已发送，准备注册")

        has_session = bool(child_account_service.decrypt_secret(child.access_token_encrypted) or child_account_service.decrypt_secret(child.session_token_encrypted))
        should_register = not (reuse_existing and has_session and child_account_service.decrypt_secret(child.password_encrypted))
        pickup_url = parsed.get("pickup_url") or ""
        if not pickup_url and child.mail_raw:
            pickup_url = parse_mail_line(child.mail_raw).get("pickup_url") or ""
        cf_config = await self._cf_config(db_session)
        use_cloudflare = (not pickup_url) and bool(cf_config["admin_password"])
        if not pickup_url and not use_cloudflare:
            error = "请先在系统中心配置 Cloudflare 邮箱，或输入 email----pickup_url"
            await self._progress(db_session, child, job_id=job_id, stage="mail_missing", message=error, error=error, error_code="mail_missing")
            return {"success": False, "error": error, "error_code": "mail_missing", "status": "mail_missing"}

        if self._cancelled(job_id):
            return {"success": False, "error": "已取消", "error_code": "cancelled", "status": "cancelled"}

        invite_url = ""

        browser_mode = "register" if should_register else "relogin"
        await self._progress(
            db_session,
            child,
            job_id=job_id,
            stage="browser",
            message=(
                ("正在打开浏览器走注册" if browser_mode == "register" else "正在打开浏览器复用登录")
                + (f" {invite_url}" if invite_url else "（默认登录页）")
            ),
        )

        def on_stage(stage: str, message: str) -> None:
            onboard_jobs.note(job_id, stage, message)

        try:
            browser_result = await asyncio.to_thread(
                self._run_browser,
                email=email,
                password=password,
                pickup_url=pickup_url,
                phone=phone,
                sms_url=sms_url,
                proxy=self._child_proxy(child, team),
                start_url=invite_url,
                mode=browser_mode,
                team_name=str(team.team_name or ""),
                use_cloudflare=use_cloudflare,
                cf_base_url=cf_config["base_url"],
                cf_address=cf_config["address"],
                cf_admin_password=cf_config["admin_password"],
                on_stage=on_stage,
                phone_source=phone_source,
            )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            code = classify_onboard_error(error, stage="browser")
            await child_account_service.record_event(
                db_session,
                email=email,
                action=browser_mode,
                team_id=team.id,
                child_id=child.id,
                success=False,
                detail=error,
                error_code=code,
            )
            await self._progress(db_session, child, job_id=job_id, stage="browser_failed", message=error, error=error, error_code=code)
            return {"success": False, "error": error, "error_code": code, "status": "browser_failed"}

        if not isinstance(browser_result, dict):
            error = f"浏览器流程返回了无效结果: {type(browser_result).__name__}"
            await self._progress(db_session, child, job_id=job_id, stage="browser_failed", message=error, error=error, error_code="browser_await_bug")
            return {"success": False, "error": error, "error_code": "browser_await_bug", "status": "browser_failed"}

        self._apply_browser_phone(child, browser_result, phone, sms_url)
        if not browser_result.get("ok"):
            error = browser_result.get("error") or "浏览器流程失败"
            code = browser_result.get("error_code") or classify_onboard_error(error, stage="browser")
            await child_account_service.record_event(
                db_session,
                email=email,
                action=browser_mode,
                team_id=team.id,
                child_id=child.id,
                success=False,
                detail=error,
                error_code=code,
            )
            await self._progress(db_session, child, job_id=job_id, stage="browser_failed", message=error, error=error, error_code=code)
            return {"success": False, "error": error, "error_code": code, "status": "browser_failed"}

        await child_account_service.save_tokens(db_session, child, {
            "access_token": browser_result.get("access_token") or "",
            "refresh_token": browser_result.get("refresh_token") or "",
            "session_token": browser_result.get("session_token") or "",
            "id_token": browser_result.get("id_token") or "",
            "account_id": team.account_id if is_workspace_account_id(team.account_id) else (browser_result.get("account_id") or ""),
            "client_id": browser_result.get("client_id") or "",
        })
        if browser_result.get("password"):
            child.password_encrypted = child_account_service.encrypt_secret(browser_result["password"])

        joined = False
        last_error = ""
        await self._progress(db_session, child, job_id=job_id, stage="reconciling", message="注册完成，正在对账是否已加入")
        for _ in range(8):
            if self._cancelled(job_id):
                return {"success": False, "error": "已取消", "error_code": "cancelled", "status": "cancelled"}
            try:
                joined = await self._confirm_joined(db_session, team.id, email)
                if joined:
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            await asyncio.sleep(5)

        if not joined:
            detail = last_error or "邀请后对账未看到该成员，未推送 Sub2API"
            code = classify_onboard_error(detail, stage="reconcile")
            await child_account_service.record_event(
                db_session,
                email=email,
                action="reconcile",
                team_id=team.id,
                child_id=child.id,
                success=False,
                detail=detail,
                error_code=code,
            )
            await self._progress(db_session, child, job_id=job_id, stage="not_joined", message=detail, error=detail, error_code=code)
            return {"success": False, "error": detail, "error_code": code, "status": "not_joined"}

        mapping = await self._mapping(db_session, team.id, email)
        await child_account_service.mark_active(db_session, child, team, mapping=mapping)
        return await self._finish_callable_child(db_session, child, team, email=email, job_id=job_id, phone_line=phone_line)

    async def _oauth_failed(
        self,
        db_session: AsyncSession,
        child: ChildAccount,
        team: Optional[Team],
        *,
        email: str,
        job_id: Optional[str],
        error: str,
        error_code: str,
    ) -> Dict[str, Any]:
        await child_account_service.record_event(
            db_session,
            email=email,
            action="oauth",
            team_id=team.id if team else None,
            child_id=child.id,
            success=False,
            detail=error,
            error_code=error_code,
        )
        await self._progress(
            db_session,
            child,
            job_id=job_id,
            stage="oauth_failed",
            message=error,
            error=error,
            error_code=error_code,
        )
        return {
            "success": False,
            "error": error,
            "error_code": error_code,
            "status": "oauth_failed",
            "child": child_account_service.serialize(child),
        }

    async def _push_child(
        self,
        db_session: AsyncSession,
        child: ChildAccount,
        team: Optional[Team],
        *,
        email: str,
        job_id: Optional[str],
    ) -> Dict[str, Any]:
        verb = self._done_verb(team)
        await self._progress(db_session, child, job_id=job_id, stage="pushing", message=f"{verb}，正在推送可用会话到 Sub2API")
        try:
            push_result = await sub2api_service.import_session(
                db_session,
                email=email,
                access_token=child_account_service.decrypt_secret(child.access_token_encrypted),
                refresh_token=child_account_service.decrypt_secret(child.refresh_token_encrypted),
                id_token=child_account_service.decrypt_secret(child.id_token_encrypted),
                account_id=child.account_id or "",
                client_id=child.client_id or "",
                existing_id=child.sub2api_account_id,
                team=team,
                proxy_url=child.proxy or ((team.proxy if team else "") or ""),
                role="child",
                name_style="free" if team is None else "",
            )
        except Exception as exc:  # noqa: BLE001
            error = f"{verb}，但推送 Sub2API 失败: {exc}"
            await child_account_service.record_event(
                db_session,
                email=email,
                action="push",
                team_id=team.id if team else None,
                child_id=child.id,
                success=False,
                detail=str(exc),
                error_code="push_failed",
            )
            await self._progress(db_session, child, job_id=job_id, stage="push_failed", message=error, error=error, error_code="push_failed")
            return {
                "success": False,
                "error": error,
                "error_code": "push_failed",
                "status": "push_failed",
                "child": child_account_service.serialize(child),
            }
        if push_result.get("account_id"):
            child.sub2api_account_id = int(push_result["account_id"])
        await child_account_service.save_probe(db_session, child, push_result.get("probe"))
        await child_account_service.record_event(
            db_session,
            email=email,
            action="push",
            team_id=team.id if team else None,
            child_id=child.id,
            success=True,
            detail=push_result.get("strategy") or "pushed",
        )
        return {"success": True, "push": push_result}

    async def _oauth_child(
        self,
        db_session: AsyncSession,
        child: ChildAccount,
        team: Optional[Team],
        *,
        email: str,
        job_id: Optional[str],
        phone_line: str = "",
    ) -> Dict[str, Any]:
        from app.utils.jwt_parser import JWTParser

        oauth_phone = child.phone or ""
        oauth_sms = child.sms_url or ""
        phone_source = None
        if not str(phone_line or "").strip():
            oauth_phone, oauth_sms, phone_source = self._bind_phone("", job_id)

        password = child_account_service.decrypt_secret(child.password_encrypted)
        if not password:
            return await self._oauth_failed(
                db_session,
                child,
                team,
                email=email,
                job_id=job_id,
                error=f"{self._done_verb(team)}，但没有密码，无法自动走 Codex 授权",
                error_code="oauth_missing_password",
            )
        pickup_url = parse_mail_line(child.mail_raw).get("pickup_url") or ""
        cf_config = await self._cf_config(db_session)
        use_cloudflare = (not pickup_url) and bool(cf_config["admin_password"])
        if not pickup_url and not use_cloudflare:
            return await self._oauth_failed(
                db_session,
                child,
                team,
                email=email,
                job_id=job_id,
                error=f"{self._done_verb(team)}，但没有邮箱读码配置，无法自动授权",
                error_code="mail_missing",
            )
        if self._cancelled(job_id):
            return {"success": False, "error": "已取消", "error_code": "cancelled", "status": "cancelled"}

        await self._progress(db_session, child, job_id=job_id, stage="oauth", message="正在打开 Codex 授权链接注册或登录，换可用 refresh token")
        auth = chatgpt_service.create_oauth_authorize_url(
            client_id=oauth_sessions.CLIENT_ID,
            redirect_uri=oauth_sessions.REDIRECT_URI,
            login_hint=email,
        )

        def on_stage(stage: str, message: str) -> None:
            onboard_jobs.note(job_id, stage, message)

        try:
            browser = await asyncio.to_thread(
                self._run_oauth_browser,
                email=email,
                password=password,
                authorize_url=auth["authorize_url"],
                proxy=self._child_proxy(child, team),
                pickup_url=pickup_url,
                phone=oauth_phone,
                sms_url=oauth_sms,
                use_cloudflare=use_cloudflare,
                cf_base_url=cf_config["base_url"],
                cf_address=cf_config["address"],
                cf_admin_password=cf_config["admin_password"],
                allow_signup=team is None,
                on_stage=on_stage,
                phone_source=phone_source,
            )
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
            code = classify_onboard_error(error, stage="oauth")
            return await self._oauth_failed(
                db_session, child, team, email=email, job_id=job_id, error=error, error_code=code
            )
        self._apply_browser_phone(child, browser, oauth_phone, oauth_sms)
        if not browser.get("ok"):
            error = str(browser.get("error") or "自动授权失败")
            code = str(browser.get("error_code") or classify_onboard_error(error, stage="oauth"))
            return await self._oauth_failed(
                db_session, child, team, email=email, job_id=job_id, error=error, error_code=code
            )

        parsed = oauth_sessions.parse_oauth_callback(str(browser.get("callback_url") or ""))
        if not parsed["code"]:
            return await self._oauth_failed(
                db_session,
                child,
                team,
                email=email,
                job_id=job_id,
                error="授权回调里没有 code",
                error_code="oauth_failed",
            )
        if parsed["state"] and parsed["state"] != auth["state"]:
            return await self._oauth_failed(
                db_session,
                child,
                team,
                email=email,
                job_id=job_id,
                error="授权 state 不匹配",
                error_code="oauth_failed",
            )

        exchange = await chatgpt_service.exchange_oauth_code(
            code=parsed["code"],
            client_id=oauth_sessions.CLIENT_ID,
            redirect_uri=oauth_sessions.REDIRECT_URI,
            code_verifier=auth["code_verifier"],
            db_session=db_session,
            identifier=f"oauth_{email}",
        )
        if not exchange.get("success"):
            error = str(exchange.get("error") or "兑换 token 失败")
            return await self._oauth_failed(
                db_session,
                child,
                team,
                email=email,
                job_id=job_id,
                error=error,
                error_code=classify_onboard_error(error, stage="oauth"),
            )
        if not exchange.get("refresh_token"):
            return await self._oauth_failed(
                db_session,
                child,
                team,
                email=email,
                job_id=job_id,
                error="授权成功但没有 refresh_token，未推送",
                error_code="oauth_no_refresh",
            )

        access_token = str(exchange.get("access_token") or "")
        id_token = str(exchange.get("id_token") or "")
        token_email = ""
        jwt = JWTParser()
        for token in (access_token, id_token):
            if (token or "").count(".") < 2:
                continue
            token_email = normalize_email(jwt.extract_email(token) or "")
            if token_email:
                break
        if token_email and token_email != normalize_email(email):
            error = f"登录的是 {token_email}，不是 {email}"
            return await self._oauth_failed(
                db_session,
                child,
                team,
                email=email,
                job_id=job_id,
                error=error,
                error_code="oauth_identity_mismatch",
            )

        await child_account_service.save_tokens(db_session, child, {
            "access_token": access_token,
            "refresh_token": exchange.get("refresh_token") or "",
            "id_token": id_token,
            "client_id": oauth_sessions.CLIENT_ID,
            "account_id": (
                team.account_id
                if team is not None and is_workspace_account_id(team.account_id)
                else (child.account_id or "")
            ),
        })
        await child_account_service.record_event(
            db_session,
            email=email,
            action="oauth",
            team_id=team.id if team else None,
            child_id=child.id,
            success=True,
            detail="codex-oauth",
        )
        return {"success": True}

    async def _finish_callable_child(
        self,
        db_session: AsyncSession,
        child: ChildAccount,
        team: Optional[Team],
        *,
        email: str,
        job_id: Optional[str],
        phone_line: str = "",
    ) -> Dict[str, Any]:
        oauth_ran = False
        if not child_account_service.decrypt_secret(child.refresh_token_encrypted):
            oauth_result = await self._oauth_child(db_session, child, team, email=email, job_id=job_id, phone_line=phone_line)
            if not oauth_result.get("success"):
                return oauth_result
            oauth_ran = True
        else:
            await self._progress(db_session, child, job_id=job_id, stage="oauth", message="已有 refresh token，跳过 Codex 授权")
        push_result = await self._push_child(db_session, child, team, email=email, job_id=job_id)
        if not push_result.get("success"):
            return push_result
        await child_account_service.set_progress(db_session, child, stage="done", job_id=job_id, clear_error=True)
        await db_session.commit()
        verb = self._done_verb(team)
        message = f"{email} {verb}、授权并推送到 Sub2API" if oauth_ran else f"{email} {verb}并推送到 Sub2API"
        return {
            "success": True,
            "status": "free" if team is None else "active",
            "message": message,
            "child": child_account_service.serialize(child),
            "push": push_result.get("push"),
            "oauth": oauth_ran,
        }

    async def _register_free_account_impl(
        self,
        db_session: AsyncSession,
        *,
        email_line: str,
        phone_line: str = "",
        proxy: str = "",
        password: str = "",
        job_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        from app.services.settings import settings_service

        parsed = parse_mail_line(email_line)
        email = normalize_email(parsed["email"] or email_line)
        if not email or "@" not in email:
            raise ValueError("请输入有效邮箱")

        existing = await child_account_service.get_by_email(db_session, email)
        if existing and existing.status in ACTIVE_CHILD_STATUSES and existing.current_team_id:
            return {
                "success": False,
                "error": f"{email} 已是 Team 席位号，请走拉进 Team",
                "error_code": "already_on_team",
                "status": "blocked",
                "child": child_account_service.serialize(existing),
            }
        if existing and existing.status == CHILD_STATUS_FREE and child_account_service.decrypt_secret(existing.refresh_token_encrypted):
            return await self._finish_callable_child(db_session, existing, None, email=email, job_id=job_id, phone_line=phone_line)

        if self._cancelled(job_id):
            return {"success": False, "error": "已取消", "error_code": "cancelled", "status": "cancelled"}

        phone, sms_url, _phone_source = self._bind_phone(phone_line, job_id)
        saved_proxy = (await settings_service.get_setting(db_session, "free_account_proxy", "")).strip()
        child_proxy = proxy or (existing.proxy if existing else "") or saved_proxy
        if child_proxy:
            child_proxy = normalize_proxy_url(child_proxy) or child_proxy
        if not child_proxy:
            return {
                "success": False,
                "error": "免费号需要静态 ISP。表单里填，或到系统中心保存默认免费号代理",
                "error_code": "proxy_missing",
                "status": "blocked",
            }
        if existing:
            if str(phone_line or "").strip():
                phone = phone or existing.phone or ""
                sms_url = sms_url or existing.sms_url or ""
            password = password or child_account_service.decrypt_secret(existing.password_encrypted)

        child = await child_account_service.upsert_from_input(
            db_session,
            email=email,
            password=password,
            mail_raw=parsed.get("raw") or email_line,
            phone=phone,
            sms_url=sms_url,
            proxy=child_proxy,
        )
        password = password or child_account_service.decrypt_secret(child.password_encrypted)
        if not password:
            password = _random_password()
            child.password_encrypted = child_account_service.encrypt_secret(password)
        await self._progress(db_session, child, job_id=job_id, stage="checking", message="正在走 Codex 授权链接注册免费号")

        pickup_url = parsed.get("pickup_url") or ""
        if not pickup_url and child.mail_raw:
            pickup_url = parse_mail_line(child.mail_raw).get("pickup_url") or ""
        cf_config = await self._cf_config(db_session)
        use_cloudflare = (not pickup_url) and bool(cf_config["admin_password"])
        if not pickup_url and not use_cloudflare:
            error = "请先在系统中心配置 Cloudflare 邮箱，或输入 email----pickup_url"
            await self._progress(db_session, child, job_id=job_id, stage="mail_missing", message=error, error=error, error_code="mail_missing")
            return {"success": False, "error": error, "error_code": "mail_missing", "status": "mail_missing"}

        if self._cancelled(job_id):
            return {"success": False, "error": "已取消", "error_code": "cancelled", "status": "cancelled"}

        await child_account_service.mark_free(db_session, child)
        await child_account_service.record_event(
            db_session,
            email=email,
            action="register",
            child_id=child.id,
            success=True,
            detail="free-oauth-register",
        )
        return await self._finish_callable_child(db_session, child, None, email=email, job_id=job_id, phone_line=phone_line)

    async def kick_to_standby(
        self,
        db_session: AsyncSession,
        *,
        team_id: int,
        email: str,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        return await self._kick_to_standby_impl(
            db_session, team_id=team_id, email=email, user_id=user_id
        )

    async def _kick_joined_and_verify(
        self,
        db_session: AsyncSession,
        *,
        team: Team,
        target: str,
        live_item: Optional[Dict[str, Any]],
        user_id: Optional[str],
    ) -> Dict[str, Any]:
        ids = chatgpt_member_ids(user_id, live_item or {})
        if not ids:
            return {
                "success": False,
                "error": f"{target} 还在 Team 里，但没有可踢的 user_id",
                "error_code": "kick_missing_user_id",
            }
        last: Dict[str, Any] = {"success": False, "error": "未执行踢人"}
        for candidate in ids:
            last = await team_service.delete_team_member(team.id, candidate, db_session, email=target)
            if not last.get("success"):
                logger.warning("踢人 ID %s 失败: %s", candidate, last.get("error"))
                continue
            live, still = await self._lookup_live_member(
                db_session, team.id, target, retries=2, interval=1.5
            )
            if live.get("success") is False:
                return {
                    "success": False,
                    "error": f"踢人请求已发出，但无法核对成员列表: {live.get('error') or '读取失败'}。没有把子号标成 standby。",
                    "error_code": "kick_unverified",
                }
            if still is None or still.get("status") == "invited":
                last["verified"] = True
                last["kicked_user_id"] = candidate
                return last
            logger.warning(
                "踢人接口成功但 %s 仍在成员列表 id=%s already_removed=%s",
                target,
                candidate,
                last.get("already_removed"),
            )
        return {
            "success": False,
            "error": f"{target} 没有踢掉，ChatGPT 里还在。不要信刚才的成功提示。",
            "error_code": "kick_not_removed",
        }

    async def _kick_to_standby_impl(
        self,
        db_session: AsyncSession,
        *,
        team_id: int,
        email: str,
        user_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        team = await self._load_team(db_session, team_id)
        self._team_proxy(team)
        target = normalize_email(email)
        child = await child_account_service.get_by_email(db_session, target)

        live, live_item = await self._lookup_live_member(db_session, team_id, target)
        if live.get("success") is False:
            return {"success": False, "error": f"{live.get('error') or '读取成员失败'}，未执行踢人/撤回"}
        live_status = (live_item or {}).get("status")
        if live_item:
            live_id = ChatGPTService.pick_user_id(live_item)
            if live_id:
                user_id = live_id
            elif live_item.get("user_id"):
                user_id = live_item.get("user_id")

        should_revoke = live_status == "invited" or (
            live_item is None and (child is None or child.status != CHILD_STATUS_ACTIVE)
        )
        if should_revoke:
            result = await team_service.revoke_team_invite(team_id, target, db_session)
            if not result.get("success") and live_item is None:
                result = {
                    "success": True,
                    "message": f"{target} 上游已看不到邀请，按已撤回处理",
                    "already_absent": True,
                }
            if not result.get("success"):
                await child_account_service.record_event(
                    db_session,
                    email=target,
                    action="revoke",
                    team_id=team.id,
                    child_id=child.id if child else None,
                    success=False,
                    detail=result.get("error") or "撤回邀请失败",
                    error_code="revoke_failed",
                )
                await db_session.commit()
                return {"success": False, "error": result.get("error") or "撤回邀请失败"}

            mapping = await self._mapping(db_session, team.id, target)
            if child:
                await child_account_service.mark_unused(db_session, child, mapping=mapping, stage="revoked")
            await child_account_service.record_event(
                db_session,
                email=target,
                action="revoke",
                team_id=team.id,
                child_id=child.id if child else None,
                success=True,
                detail="已撤回邀请，子号回到未使用",
            )
            await db_session.commit()
            return {
                "success": True,
                "status": "revoked",
                "message": f"{target} 已撤回邀请，子号回到未使用",
                "child": child_account_service.serialize(child) if child else None,
            }

        if live_item is None and not user_id:
            result = {
                "success": True,
                "message": f"{target} 重试后仍不在成员列表，按已不在 Team 处理",
                "already_absent": True,
            }
        else:
            result = await self._kick_joined_and_verify(
                db_session,
                team=team,
                target=target,
                live_item=live_item,
                user_id=user_id,
            )

        if not result.get("success"):
            await child_account_service.record_event(
                db_session,
                email=target,
                action="kick",
                team_id=team.id,
                child_id=child.id if child else None,
                success=False,
                detail=result.get("error") or "踢人失败",
                error_code=result.get("error_code") or "kick_failed",
            )
            await db_session.commit()
            return {"success": False, "error": result.get("error") or "踢人失败", "error_code": result.get("error_code") or "kick_failed"}

        mapping = await self._mapping(db_session, team.id, target)
        if child:
            await child_account_service.mark_standby(db_session, child, mapping=mapping)
        await child_account_service.record_event(
            db_session,
            email=target,
            action="kick",
            team_id=team.id,
            child_id=child.id if child else None,
            success=True,
            detail="已踢出并保留子号",
        )
        await db_session.commit()
        vacancy = result.get("vacancy")
        message = f"{target} 已踢出，子号进入 standby"
        summary = summarize_for_message(vacancy)
        if summary:
            message = f"{message}。{summary}"
        return {
            "success": True,
            "status": "standby",
            "message": message,
            "child": child_account_service.serialize(child) if child else None,
            "vacancy": vacancy,
        }

    async def kick_and_refill(
        self,
        db_session: AsyncSession,
        *,
        team_id: int,
        email: str,
        email_line: str = "",
        phone_line: str = "",
        proxy: str = "",
        child_id: Optional[int] = None,
        force_refill: bool = False,
        job_id: Optional[str] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """踢指定子号进 standby，过 vacancy 闸后再拉人。不是到期轮转。"""
        team = await self._load_team(db_session, team_id)
        kick_target_email = normalize_email(email)
        if not kick_target_email:
            return {"success": False, "error": "缺少要踢的子号邮箱", "error_code": "rotate_email_missing"}
        kick_result = await self.kick_to_standby(db_session, team_id=team.id, email=kick_target_email)
        if not kick_result.get("success"):
            return kick_result

        await child_account_service.record_event(
            db_session,
            email=kick_target_email,
            action="rotate",
            team_id=team.id,
            child_id=(kick_result.get("child") or {}).get("id") if isinstance(kick_result.get("child"), dict) else None,
            success=True,
            detail=f"{reason + ': ' if reason else ''}kicked {kick_target_email}",
        )
        await db_session.commit()

        vacancy = kick_result.get("vacancy")
        if not force_refill and not is_safe_to_refill(vacancy):
            summary = summarize_for_message(vacancy) or "踢人回执不能证明席位已释放"
            return {
                "success": False,
                "error": f"已踢出 {kick_target_email}，但{summary}。已停止自动补位，核对 Billing 后可勾选强制补位。",
                "error_code": "vacancy_not_safe_to_refill",
                "needs_confirm": True,
                "kick": kick_result,
                "vacancy": vacancy,
                "rotated": True,
            }

        replacement = None
        if child_id:
            replacement = await child_account_service.get_by_id(db_session, child_id)
        elif email_line:
            replacement_email = normalize_email(parse_mail_line(email_line)["email"] or email_line)
            replacement = await child_account_service.get_by_email(db_session, replacement_email)

        if replacement is None and not email_line:
            standby = await child_account_service.list_accounts(db_session, status="standby")
            for item in standby:
                if item.email != kick_target_email:
                    if item.kicked_at and (get_now() - item.kicked_at).total_seconds() < KICK_COOLDOWN_SECONDS:
                        continue
                    replacement = item
                    break

        if replacement is None and not str(email_line or "").strip():
            invite_line = ""
        elif replacement is None:
            invite_line = email_line
        else:
            invite_line = email_line or (replacement.mail_raw or replacement.email)
        invite_result = await self.invite_and_onboard(
            db_session,
            team_id=team.id,
            email_line=invite_line,
            phone_line=phone_line,
            proxy=proxy or (replacement.proxy if replacement else ""),
            reuse_existing=True,
            force=force_refill,
            job_id=job_id,
        )
        if not invite_result.get("success"):
            return {
                "success": False,
                "error": invite_result.get("error") or "补位失败，未完成轮转",
                "kick": kick_result,
                "invite": invite_result,
            }
        await db_session.commit()
        vacancy = kick_result.get("vacancy")
        message = f"已踢出 {kick_target_email} 并补入 {(invite_result.get('child') or {}).get('email')}"
        summary = summarize_for_message(vacancy)
        if summary:
            message = f"{message}。{summary}"
        return {
            "success": True,
            "message": message,
            "kick": kick_result,
            "invite": invite_result,
            "vacancy": vacancy,
            "rotated": True,
        }

    async def rotate_one(
        self,
        db_session: AsyncSession,
        *,
        team_id: int,
        email_line: str = "",
        phone_line: str = "",
        proxy: str = "",
        child_id: Optional[int] = None,
        force_refill: bool = False,
    ) -> Dict[str, Any]:
        team = await self._load_team(db_session, team_id)
        due = await child_account_service.list_due_accounts(db_session, team_id=team.id)
        if not due:
            return {"success": False, "error": "该 Team 没有到期需要踢出的子号"}
        return await self.kick_and_refill(
            db_session,
            team_id=team.id,
            email=due[0].email,
            email_line=email_line,
            phone_line=phone_line,
            proxy=proxy,
            child_id=child_id,
            force_refill=force_refill,
            reason="cycle_due",
        )


onboard_service = OnboardService()
