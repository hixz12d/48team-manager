"""Invite then Playwright onboard. Vacancy still gates refill; Sub2API push stays out."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.application.identity import ensure_membership
from app.application.jobs import browser as browser_slot
from app.application.operations import operation_store
from app.application.reauth import load_cf_config
from app.application.resources import hme as hme_service
from app.application.resources.phones import phone_source_for
from app.application.resources.proxies import proxy_profile_service
from app.application.tokens import auth_service, decrypt_secret, encrypt_secret
from app.application.workspaces import workspace_service
from app.core.time import utcnow
from app.domain.automation import WORKSPACE_LOCK_ACTIONS
from app.domain.identity import (
    LOCAL_PURPOSE_CHILD,
    LOCAL_PURPOSE_MOTHER,
    MEMBERSHIP_STATE_INVITED,
    MEMBERSHIP_STATE_JOINED,
    OFFICIAL_ROLE_UNKNOWN,
)
from app.domain.identity.ids import normalize_email
from app.integrations.openai.member_adapter import (
    normalize_official_member,
    normalize_official_role,
    official_roles_equivalent,
    parse_invite_role,
    parse_invite_seat_intent,
    InviteSeatIntent,
    existing_invite_seat_error,
)
from app.domain.onboard import (
    JOIN_CONFIRM_ATTEMPTS,
    JOIN_CONFIRM_INTERVAL,
    KICK_COOLDOWN_SECONDS,
    classify_onboard_error,
    random_password,
)
from app.integrations.mail.otp import parse_mail_line
from app.integrations.openai.browser.onboard import run_browser_onboard
from app.integrations.sms.client import parse_phone_line, require_proxy
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership

logger = logging.getLogger(__name__)


def serialize_child(account: Account | None) -> dict[str, Any] | None:
    if account is None:
        return None
    return {
        "id": account.id,
        "email": account.email,
        "operational_state": account.operational_state,
        "local_purpose": account.local_purpose,
        "proxy": account.proxy,
    }


class OnboardService:
    def __init__(self, *, workspaces=None, browser=None):
        self.workspaces = workspaces or workspace_service
        self.browser = browser or run_browser_onboard

    async def _progress(
        self,
        db: AsyncSession,
        *,
        job_id: str | None,
        stage: str,
        message: str,
        error: str = "",
        error_code: str = "",
    ) -> None:
        if not job_id:
            return
        op = await operation_store.get_by_public_id(db, job_id)
        if op is None:
            return
        await operation_store.note(db, op, stage, message, error=error, error_code=error_code)

    async def _load_workspace(self, db: AsyncSession, workspace_id: int) -> Workspace | None:
        return (
            await db.execute(
                select(Workspace)
                .options(selectinload(Workspace.owner_account), selectinload(Workspace.memberships))
                .where(Workspace.id == int(workspace_id))
            )
        ).scalar_one_or_none()

    def _child_proxy(self, child: Account, workspace: Workspace | None = None) -> str:
        owner = workspace.owner_account if workspace is not None else None
        return require_proxy(child.proxy or (owner.proxy if owner else ""), "子号浏览器/接码")

    async def _confirm_joined(self, db: AsyncSession, workspace: Workspace, email: str) -> dict[str, Any] | None:
        members = await self.workspaces.get_members(db, workspace)
        if not members.get("success"):
            raise RuntimeError(members.get("error") or "对账成员列表失败")
        target = normalize_email(email)
        for item in members.get("members") or members.get("items") or []:
            adapted = normalize_official_member(item, default_state="joined")
            if adapted and adapted["email"] == target:
                from app.persistence.models.identity import WorkspaceOfficialMemberSnapshot

                snapshot = await db.scalar(select(WorkspaceOfficialMemberSnapshot).where(
                    WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id,
                    WorkspaceOfficialMemberSnapshot.normalized_email == target,
                ))
                if snapshot is None:
                    snapshot = WorkspaceOfficialMemberSnapshot(workspace_id=workspace.id, normalized_email=target)
                    db.add(snapshot)
                snapshot.remote_state = "joined"
                snapshot.official_role = normalize_official_role(adapted.get("role"))
                snapshot.official_user_id = adapted.get("user_id")
                snapshot.seat_type = adapted.get("seat_type")
                snapshot.fetched_at = utcnow()
                await db.flush()
                return adapted
        return None

    def _bind_phone(self, phone_line: str, job_id: str | None, account_id: int | None = None):
        manual = str(phone_line or "").strip()
        if manual:
            number, sms_url = parse_phone_line(manual)
            return number, sms_url, None
        if not job_id:
            return "", "", None
        return "", "", phone_source_for(job_id, account_id, purpose="signup")

    def _apply_browser_phone(self, child: Account, browser_result: dict[str, Any], fallback_phone: str = "", fallback_sms: str = "") -> None:
        phone = str(browser_result.get("phone") or fallback_phone or "").strip()
        sms_url = str(browser_result.get("sms_url") or fallback_sms or "").strip()
        if phone:
            child.phone = phone
        if sms_url:
            child.sms_url = sms_url

    async def _upsert_child(
        self,
        db: AsyncSession,
        *,
        email: str,
        password: str = "",
        mail_raw: str = "",
        phone: str = "",
        sms_url: str = "",
        proxy: str = "",
        proxy_source: str = "",
        sub2api_proxy_id: int | None = None,
        proxy_instance_key: str = "",
        proxy_profile_id: int | None = None,
        status: str = "invited",
    ) -> Account:
        target = normalize_email(email)
        child = (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none()
        if child is None:
            child = Account(
                email=target,
                official_plan="unknown",
                local_purpose=LOCAL_PURPOSE_CHILD,
                operational_state="available",
            )
            db.add(child)
            await db.flush()
        if password:
            child.password_encrypted = encrypt_secret(password)
        if mail_raw:
            child.mail_raw = mail_raw
        if phone:
            child.phone = phone
        if sms_url:
            child.sms_url = sms_url
        if proxy:
            child.proxy = proxy
            child.proxy_source = proxy_source or "legacy"
            child.sub2api_proxy_id = sub2api_proxy_id
            child.proxy_instance_key = proxy_instance_key or None
            child.proxy_profile_id = proxy_profile_id
        if child.local_purpose != "mother":
            child.local_purpose = LOCAL_PURPOSE_CHILD if status != "standby" else child.local_purpose
        await db.flush()
        return child

    async def invite_and_onboard(
        self,
        db: AsyncSession,
        *,
        workspace_id: int,
        email_line: str,
        phone_line: str = "",
        proxy: str = "",
        proxy_source: str = "",
        sub2api_proxy_id: int | None = None,
        proxy_instance_key: str = "",
        password: str = "",
        reuse_existing: bool = True,
        skip_invite: bool = False,
        force: bool = False,
        job_id: str | None = None,
        in_test: bool = False,
        role: str = "owner",
        seat_intent: str = "workspace_default",
        oauth_signup: bool = False,
        browser_executable: str = "",
    ) -> dict[str, Any]:
        seat_intent = parse_invite_seat_intent(seat_intent).value
        claimed = None
        if oauth_signup:
            busy = await operation_store.active_for_workspace(
                db, workspace_id, actions=WORKSPACE_LOCK_ACTIONS, exclude_public_id=job_id,
            )
            browser_busy = await operation_store.browser_busy(db)
            if busy or (browser_busy and browser_busy.public_id != job_id):
                return {"success": False, "error_code": "operation_conflict", "error": "已有任务运行，未领取 HME"}
            from app.application.invitation_flow import prepare

            email_line, blocked = await prepare(
                self, db, workspace_id=workspace_id, email_line=email_line,
                phone_line=phone_line, role=role, seat_intent=seat_intent, skip_invite=skip_invite,
            )
            if blocked:
                return blocked
        try:
            email_line, claimed = await hme_service.maybe_claim_alias(
                db,
                email_line,
                job_id=job_id or "",
                purpose="onboard",
                workspace_id=workspace_id,
            )
            if claimed:
                await self._progress(db, job_id=job_id, stage="hme", message=f"已领取 HME 别名 {claimed.email}")
            result = await self._invite_and_onboard_impl(
                db,
                workspace_id=workspace_id,
                email_line=email_line,
                phone_line=phone_line,
                proxy=proxy,
                proxy_source=proxy_source,
                sub2api_proxy_id=sub2api_proxy_id,
                proxy_instance_key=proxy_instance_key,
                password=password,
                reuse_existing=reuse_existing,
                skip_invite=skip_invite,
                force=force,
                job_id=job_id,
                claimed=claimed,
                in_test=in_test,
                role=role,
                seat_intent=seat_intent,
                oauth_signup=oauth_signup,
                browser_executable=browser_executable,
            )
            label = ""
            if claimed and result.get("success"):
                workspace = await self._load_workspace(db, workspace_id)
                cfg = await hme_service.load_config(db)
                label = hme_service.resolve_workspace_tag(workspace, cfg.team_tag_map)
            await hme_service.finalize_claim(db, claimed, result, label)
            if oauth_signup and result.get("success"):
                from app.application.invitation_flow import authorize_joined

                result = await authorize_joined(
                    self, db, result, workspace_id=workspace_id, phone_line=phone_line,
                    role=role, seat_intent=seat_intent, job_id=job_id, executable_path=browser_executable,
                )
            return result
        except hme_service.HmeError as exc:
            await hme_service.finalize_claim(db, claimed, {"success": False, "error_code": exc.code})
            await self._progress(db, job_id=job_id, stage="hme_failed", message=str(exc), error=str(exc), error_code=exc.code)
            return {"success": False, "error": str(exc), "error_code": exc.code, "status": "hme_failed"}
        except Exception:
            await hme_service.finalize_claim(db, claimed, {"success": False})
            raise

    async def _invite_and_onboard_impl(
        self,
        db: AsyncSession,
        *,
        workspace_id: int,
        email_line: str,
        phone_line: str = "",
        proxy: str = "",
        proxy_source: str = "",
        sub2api_proxy_id: int | None = None,
        proxy_instance_key: str = "",
        password: str = "",
        reuse_existing: bool = True,
        skip_invite: bool = False,
        force: bool = False,
        job_id: str | None = None,
        claimed=None,
        in_test: bool = False,
        role: str = "owner",
        seat_intent: str = "workspace_default",
        oauth_signup: bool = False,
        browser_executable: str = "",
    ) -> dict[str, Any]:
        requested_seat = parse_invite_seat_intent(seat_intent)
        busy = await operation_store.active_for_workspace(
            db,
            workspace_id,
            actions=WORKSPACE_LOCK_ACTIONS,
            exclude_public_id=job_id,
        )
        if busy is not None:
            return {
                "success": False,
                "error": f"Workspace {workspace_id} 已有 {busy.op_type} 任务 {busy.public_id} 在跑，避免两边同时踢拉",
                "error_code": "operation_conflict",
                "operation_id": busy.public_id,
            }

        parsed = parse_mail_line(email_line)
        email = normalize_email(parsed.get("email") or email_line)
        if not email or "@" not in email:
            return {"success": False, "error": "请输入有效邮箱", "error_code": "mail_missing", "status": "mail_missing"}

        workspace = await self._load_workspace(db, workspace_id)
        if workspace is None:
            return {"success": False, "error": f"未找到 Workspace {workspace_id}", "error_code": "workspace_not_found"}
        owner = workspace.owner_account
        require_proxy(owner.proxy if owner else "", "母号 Team API")
        requested_role = parse_invite_role(role)

        existing = (await db.execute(select(Account).where(Account.email == email))).scalar_one_or_none()
        membership = None
        if existing is not None:
            membership = (
                await db.execute(
                    select(WorkspaceMembership).where(
                        WorkspaceMembership.workspace_id == workspace.id,
                        WorkspaceMembership.account_id == existing.id,
                    )
                )
            ).scalar_one_or_none()
        if (
            requested_seat is InviteSeatIntent.WORKSPACE_DEFAULT
            and existing is not None
            and existing.operational_state == "active"
            and membership is not None
            and membership.membership_state == MEMBERSHIP_STATE_JOINED
            and decrypt_secret(existing.access_token_encrypted)
        ):
            return {
                "success": True,
                "status": "already_exists",
                "message": f"{email} 已在该 Workspace 中",
                "child": serialize_child(existing),
                "skipped": True,
            }

        if existing is not None and existing.operational_state == "standby" and not force:
            eligible = existing.next_eligible_at
            if eligible and eligible > utcnow():
                remain = int((eligible - utcnow()).total_seconds() / 60) + 1
                error = f"{email} 刚被踢出，{remain} 分钟内不要再拉进任何 Team，避免 token_revoked"
                return {"success": False, "error": error, "error_code": "kick_cooldown", "status": "blocked"}
            if eligible is None:
                cooldown = utcnow() - timedelta(seconds=KICK_COOLDOWN_SECONDS)
                if existing.updated_at and existing.updated_at > cooldown:
                    error = f"{email} 刚被踢出，冷却期内不要再拉进任何 Team，避免 token_revoked"
                    return {"success": False, "error": error, "error_code": "kick_cooldown", "status": "blocked"}

        phone, sms_url, phone_source = ("", "", None) if oauth_signup else self._bind_phone(
            phone_line, job_id, existing.id if existing else None
        )
        if proxy:
            effective_proxy_source = proxy_source or "legacy"
            effective_sub2api_proxy_id = sub2api_proxy_id
            effective_proxy_instance_key = proxy_instance_key
        elif existing and existing.proxy:
            effective_proxy_source = existing.proxy_source or "legacy"
            effective_sub2api_proxy_id = existing.sub2api_proxy_id
            effective_proxy_instance_key = existing.proxy_instance_key or ""
        else:
            effective_proxy_source = (owner.proxy_source if owner else None) or "legacy"
            effective_sub2api_proxy_id = owner.sub2api_proxy_id if owner else None
            effective_proxy_instance_key = (owner.proxy_instance_key if owner else None) or ""
        child_proxy = proxy or (existing.proxy if existing else "") or (owner.proxy if owner else "")
        frozen, _profile_id = await proxy_profile_service.freeze(
            db,
            job_id=job_id,
            form_proxy=child_proxy,
            child_proxy=existing.proxy if existing else "",
            mother_proxy=owner.proxy if owner else "",
        )
        child_proxy = frozen or child_proxy
        password = password or (decrypt_secret(existing.password_encrypted) if existing else "") or parsed.get("password") or ""
        child = await self._upsert_child(
            db,
            email=email,
            password=password,
            mail_raw=parsed.get("raw") or email_line,
            phone=phone or (existing.phone if existing else "") or "",
            sms_url=sms_url or (existing.sms_url if existing else "") or "",
            proxy=child_proxy,
            proxy_source=effective_proxy_source,
            sub2api_proxy_id=effective_sub2api_proxy_id,
            proxy_instance_key=effective_proxy_instance_key,
            proxy_profile_id=_profile_id,
        )
        password = password or decrypt_secret(child.password_encrypted)
        if oauth_signup and job_id:
            op = await operation_store.get_by_public_id(db, job_id)
            if op:
                op.email, op.account_id = email, child.id
                await db.flush()
        await self._progress(db, job_id=job_id, stage="checking", message="正在检查母号和占用")

        live, live_item = await self.workspaces.lookup_live_member(db, workspace, email)
        already_invited = bool(live_item and live_item.get("status") == "invited")
        already_joined = bool(live_item and live_item.get("status") == "joined")
        if live_item:
            seat_error = existing_invite_seat_error(requested_seat, live_item.get("seat_type"))
            if seat_error:
                return seat_error
        elif not skip_invite and not live.get("success"):
            return {"success": False, "error_code": "invite_lookup_unknown", "error": "官方成员或邀请读取失败，未发送邀请。"}
        if already_joined:
            await ensure_membership(
                db,
                workspace_id=workspace.id,
                account_id=child.id,
                official_role=normalize_official_role((live_item or {}).get("role")) or requested_role,
                membership_state=MEMBERSHIP_STATE_JOINED,
                local_purpose=LOCAL_PURPOSE_CHILD,
                joined_at=utcnow(),
            )
            child.operational_state = "active"
            if child.local_purpose != LOCAL_PURPOSE_MOTHER:
                child.local_purpose = LOCAL_PURPOSE_CHILD
            await db.flush()
            return {
                "success": True,
                "status": "already_exists",
                "message": f"{email} 已在该 Workspace 中",
                "child": serialize_child(child),
                "skipped": True,
            }

        if not password:
            password = random_password()
            child.password_encrypted = encrypt_secret(password)

        if not skip_invite and not already_invited:
            capacity = workspace.seat_limit
            occupied = None
            if live.get("success"):
                occupied = len([item for item in (live.get("members") or []) if isinstance(item, dict)])
            if capacity is not None and occupied is not None and occupied >= capacity:
                error = f"占用 {occupied}/{capacity}，不能再邀请"
                await self._progress(db, job_id=job_id, stage="blocked", message=error, error=error, error_code="team_full")
                return {"success": False, "error": error, "error_code": "team_full", "status": "blocked"}

        if skip_invite:
            await self._progress(db, job_id=job_id, stage="skip_invite", message="已跳过官方邀请，仅走浏览器入驻")
        elif already_invited:
            live_role = normalize_official_role((live_item or {}).get("role"))
            if live_role not in {"unknown", ""} and not official_roles_equivalent(live_role, requested_role):
                error = f"{email} 已有官方邀请，但角色是 {live_role}，与本次请求的 {requested_role} 不一致"
                await self._progress(db, job_id=job_id, stage="invite_role_mismatch", message=error, error=error, error_code="invite_role_mismatch")
                return {
                    "success": False,
                    "error": error,
                    "error_code": "invite_role_mismatch",
                    "status": "invite_role_mismatch",
                    "existing_role": live_role,
                    "requested_role": requested_role,
                }
            await ensure_membership(
                db,
                workspace_id=workspace.id,
                account_id=child.id,
                official_role=live_role if live_role not in {"unknown", ""} else requested_role,
                membership_state=MEMBERSHIP_STATE_INVITED,
                local_purpose=LOCAL_PURPOSE_CHILD,
            )
            await self._progress(db, job_id=job_id, stage="invited", message="邀请已存在，开始重新注册")
        else:
            await self._progress(db, job_id=job_id, stage="inviting", message="正在发送 Team 邀请")
            invite = await self.workspaces.invite_member(db, workspace.id, email, role=requested_role, seat_intent=requested_seat)
            if not invite.get("success"):
                error = invite.get("error") or "邀请失败"
                code = invite.get("error_code") or classify_onboard_error(error, stage="invite")
                await self._progress(db, job_id=job_id, stage="invite_failed", message=error, error=error, error_code=code)
                return {"success": False, "error": error, "error_code": code, "status": "invite_failed"}
            await ensure_membership(
                db,
                workspace_id=workspace.id,
                account_id=child.id,
                official_role=requested_role,
                membership_state=MEMBERSHIP_STATE_INVITED,
                local_purpose=LOCAL_PURPOSE_CHILD,
            )
            await self._progress(db, job_id=job_id, stage="invited", message="邀请已发送，准备注册")

        if oauth_signup:
            checked, official_invite = await self.workspaces.lookup_live_member(db, workspace, email)
            seat_error = existing_invite_seat_error(requested_seat, (official_invite or {}).get("seat_type"))
            if not checked.get("success") or not official_invite or seat_error or not official_roles_equivalent(official_invite.get("role"), requested_role):
                return {"success": False, "error_code": "invite_unverified", "status": "invited",
                        "error": "官方邀请、角色或席位未确认，请继续此邮箱，不会开始注册",
                        "child": serialize_child(child)}

        has_session = bool(decrypt_secret(child.access_token_encrypted) or decrypt_secret(child.session_token_encrypted))
        should_register = not (reuse_existing and has_session and decrypt_secret(child.password_encrypted))
        pickup_url = parsed.get("pickup_url") or ""
        if not pickup_url and child.mail_raw:
            pickup_url = parse_mail_line(child.mail_raw).get("pickup_url") or ""
        cf_config = await load_cf_config(db)
        use_cloudflare = (not pickup_url) and bool(cf_config["admin_password"])
        if not pickup_url and not use_cloudflare:
            error = "请先在系统中心配置 Cloudflare 邮箱，或输入 email----pickup_url"
            await self._progress(db, job_id=job_id, stage="mail_missing", message=error, error=error, error_code="mail_missing")
            return {"success": False, "error": error, "error_code": "mail_missing", "status": "mail_missing"}

        invite_url = ""
        if oauth_signup:
            from app.integrations.mail.otp import wait_for_mailbox_item, extract_invite_url

            await self._progress(db, job_id=job_id, stage="invite_mail", message="等待邀请邮件链接")
            await db.commit()
            try:
                invite_url = await asyncio.to_thread(
                    wait_for_mailbox_item, email=email, pickup_url=pickup_url,
                    proxy=self._child_proxy(child, workspace), kind="invite", timeout_sec=120,
                    cf_base_url=cf_config["base_url"], cf_address=cf_config["address"],
                    cf_admin_password=cf_config["admin_password"],
                )
            except Exception:
                invite_url = None
            if not invite_url or extract_invite_url(invite_url) != invite_url:
                return {"success": False, "error_code": "invite_link_missing", "status": "invited",
                        "error": "未收到有效邀请链接，请继续此邮箱，不会改走普通注册页",
                        "child": serialize_child(child)}
        browser_options = {"allow_sms": False, "executable_path": browser_executable, "invite_entry": True} if oauth_signup else {}

        if claimed is not None:
            await hme_service.mark_signup_started(db, claimed, stage="browser")

        browser_mode = "register" if should_register else "relogin"
        await self._progress(
            db,
            job_id=job_id,
            stage="browser",
            message="正在打开浏览器走注册" if browser_mode == "register" else "正在打开浏览器复用登录",
        )

        def on_stage(stage: str, message: str) -> None:
            if job_id:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None
                if loop is not None:
                    loop.call_soon_threadsafe(lambda: None)
                    # Progress from Playwright stays queued on the main flow; avoid sharing AsyncSession.

        if oauth_signup and not in_test:
            async def on_stage(stage: str, message: str) -> None:
                from app.application.invitation_flow import browser_progress
                await hme_service.mark_signup_started(db, claimed, stage=stage)
                await browser_progress(db, job_id, stage)
            await db.commit()

        try:
            if in_test:
                browser_result = self.browser(
                    **browser_options,
                    email=email,
                    password=password,
                    pickup_url=pickup_url,
                    phone=phone,
                    sms_url=sms_url,
                    proxy=self._child_proxy(child, workspace),
                    start_url=invite_url,
                    mode=browser_mode,
                    team_name=str(workspace.name or ""),
                    use_cloudflare=use_cloudflare,
                    cf_base_url=cf_config["base_url"],
                    cf_address=cf_config["address"],
                    cf_admin_password=cf_config["admin_password"],
                    on_stage=on_stage,
                    phone_source=phone_source,
                )
                if asyncio.iscoroutine(browser_result):
                    browser_result = await browser_result
            else:
                runner = browser_slot.run_onboard_isolated if oauth_signup else browser_slot.run_exclusive
                runner_args = () if oauth_signup else (self.browser,)
                browser_result = await runner(
                    *runner_args,
                    email=email,
                    password=password,
                    pickup_url=pickup_url,
                    phone=phone,
                    sms_url=sms_url,
                    **browser_options,
                    proxy=self._child_proxy(child, workspace),
                    start_url=invite_url,
                    mode=browser_mode,
                    team_name=str(workspace.name or ""),
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
            await self._progress(db, job_id=job_id, stage="browser_failed", message=error, error=error, error_code=code)
            return {"success": False, "error": error, "error_code": code, "status": "browser_failed", "child": serialize_child(child)}

        if asyncio.iscoroutine(browser_result):
            browser_result = await browser_result
        if not isinstance(browser_result, dict):
            error = f"浏览器流程返回了无效结果: {type(browser_result).__name__}"
            await self._progress(db, job_id=job_id, stage="browser_failed", message=error, error=error, error_code="browser_await_bug")
            return {"success": False, "error": error, "error_code": "browser_await_bug", "status": "browser_failed", "child": serialize_child(child)}

        self._apply_browser_phone(child, browser_result, phone, sms_url)
        if not browser_result.get("ok"):
            error = browser_result.get("error") or "浏览器流程失败"
            code = browser_result.get("error_code") or classify_onboard_error(error, stage="browser")
            await self._progress(db, job_id=job_id, stage="browser_failed", message=error, error=error, error_code=code)
            return {"success": False, "error": error, "error_code": code, "status": "browser_failed", "child": serialize_child(child)}

        await auth_service.apply_tokens(child, {
            "access_token": browser_result.get("access_token") or "",
            "refresh_token": browser_result.get("refresh_token") or "",
            "session_token": browser_result.get("session_token") or "",
            "id_token": browser_result.get("id_token") or "",
            "client_id": browser_result.get("client_id") or "",
        })
        if browser_result.get("password"):
            child.password_encrypted = encrypt_secret(browser_result["password"])
        await db.flush()

        joined = False
        last_error = ""
        await self._progress(db, job_id=job_id, stage="reconciling", message="注册完成，正在对账是否已加入")
        attempts = 1 if in_test else JOIN_CONFIRM_ATTEMPTS
        for _ in range(attempts):
            try:
                joined = await self._confirm_joined(db, workspace, email)
                if joined:
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            if in_test:
                break
            await asyncio.sleep(JOIN_CONFIRM_INTERVAL)

        if not joined:
            detail = last_error or "邀请后对账未看到该成员，未推送 Sub2API"
            code = classify_onboard_error(detail, stage="reconcile")
            await self._progress(db, job_id=job_id, stage="not_joined", message=detail, error=detail, error_code=code)
            return {
                "success": False,
                "error": detail,
                "error_code": code,
                "status": "not_joined",
                "child": serialize_child(child),
            }

        await ensure_membership(
            db,
            workspace_id=workspace.id,
            account_id=child.id,
            official_role=normalize_official_role(joined.get("role")) if isinstance(joined, dict) else requested_role,
            membership_state=MEMBERSHIP_STATE_JOINED,
            local_purpose=LOCAL_PURPOSE_CHILD,
            joined_at=utcnow(),
        )
        child.operational_state = "active"
        if child.local_purpose != LOCAL_PURPOSE_MOTHER:
            child.local_purpose = LOCAL_PURPOSE_CHILD
        await db.flush()
        return {
            "success": True,
            "status": "active",
            "message": f"{email} 已入组。本轮未推送 Sub2API",
            "child": serialize_child(child),
            "pushed": False,
        }

    async def pick_replacement(
        self,
        db: AsyncSession,
        *,
        skip_email: str = "",
        child_id: int | None = None,
        email_line: str = "",
        now=None,
    ) -> Account | None:
        if child_id:
            return await db.get(Account, child_id)
        if email_line:
            target = normalize_email(parse_mail_line(email_line).get("email") or email_line)
            if target:
                return (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none()
        stamp = now or utcnow()
        skip = normalize_email(skip_email)
        rows = list(
            (
                await db.execute(
                    select(Account)
                    .where(
                        Account.local_purpose == "standby",
                        Account.operational_state == "standby",
                        or_(Account.next_eligible_at.is_(None), Account.next_eligible_at <= stamp),
                    )
                    .order_by(Account.next_eligible_at.is_(None), Account.next_eligible_at.asc(), Account.id.asc())
                )
            ).scalars()
        )
        cooldown = stamp - timedelta(seconds=KICK_COOLDOWN_SECONDS)
        for item in rows:
            if skip and item.email == skip:
                continue
            if item.updated_at and item.updated_at > cooldown and item.next_eligible_at is None:
                continue
            return item
        return None

    async def refill(
        self,
        db: AsyncSession,
        *,
        workspace_id: int,
        email_line: str = "",
        phone_line: str = "",
        proxy: str = "",
        child_id: int | None = None,
        force_refill: bool = False,
        job_id: str | None = None,
        skip_email: str = "",
        in_test: bool = False,
        role: str = "owner",
    ) -> dict[str, Any]:
        replacement = await self.pick_replacement(db, skip_email=skip_email, child_id=child_id, email_line=email_line)
        if replacement is None and not str(email_line or "").strip():
            invite_line = ""
        elif replacement is None:
            invite_line = email_line
        else:
            invite_line = email_line or (replacement.mail_raw or replacement.email)
        return await self.invite_and_onboard(
            db,
            workspace_id=workspace_id,
            email_line=invite_line,
            phone_line=phone_line,
            proxy=proxy or (replacement.proxy if replacement else ""),
            reuse_existing=True,
            force=force_refill,
            job_id=job_id,
            in_test=in_test,
            role=role,
        )


onboard_service = OnboardService()
