"""Auto reauth control plane. Identity gate first; Playwright last; switch stays off."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.application.tokens import auth_service, decrypt_secret, set_auth_state
from app.application.presenters import build_auth_status
from app.application.identity import automation_gate
from app.application.jobs import browser as browser_slot
from app.application.operations import operation_store, unpack_input
from app.application.oauth_sessions import OAuthSessionError, oauth_session_store
from app.application.mailbox import mailbox_readiness_snapshot
from app.application.resources.hme import load_config as load_hme_config
from app.application.resources.proxies import proxy_profile_service
from app.application.sub2api_publish import push_refreshed_tokens_to_bound_sub2api
from app.application.settings import as_bool, get_setting_value
from app.core.config import load_settings
from app.core.time import utcnow
from app.domain.automation import (
    DEFAULT_AUTO_REAUTH_ENABLED,
    DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES,
    DEFAULT_OAUTH_CLIENT_ID,
    OAUTH_REDIRECT_URI,
)
from app.domain.identity.ids import normalize_email
from app.domain.quota import SKIP_OPERATIONAL_STATES
from app.domain.reauth import (
    auto_reauth_plan,
    clamp_auto_reauth_interval_minutes,
    reauth_backoff_at,
    reauth_terminal_status,
)
from app.integrations.mail.cloudflare import (
    CF_SETTING_ADDRESS,
    CF_SETTING_ADMIN_PASSWORD,
    CF_SETTING_BASE_URL,
    DEFAULT_CF_MAIL_ADDRESS,
    DEFAULT_CF_MAIL_BASE_URL,
)
from app.integrations.mail.otp import parse_mail_line
from app.integrations.openai import oauth_sessions
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership

logger = logging.getLogger(__name__)


async def load_cf_config(db: AsyncSession) -> dict[str, str]:
    return {
        "base_url": (await get_setting_value(db, CF_SETTING_BASE_URL, DEFAULT_CF_MAIL_BASE_URL) or DEFAULT_CF_MAIL_BASE_URL).strip(),
        "address": (await get_setting_value(db, CF_SETTING_ADDRESS, DEFAULT_CF_MAIL_ADDRESS) or DEFAULT_CF_MAIL_ADDRESS).strip(),
        "admin_password": (await get_setting_value(db, CF_SETTING_ADMIN_PASSWORD, "") or "").strip(),
    }




class ReauthService:
    async def execution_context(self, db: AsyncSession, account: Account) -> dict[str, Any]:
        proxy_account = account
        proxy_origin = "account"
        if not str(account.proxy or "").strip() and account.local_purpose == "child":
            owner = aliased(Account)
            proxy_account = (
                await db.execute(
                    select(owner)
                    .select_from(WorkspaceMembership)
                    .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
                    .join(owner, owner.id == Workspace.owner_account_id)
                    .where(
                        WorkspaceMembership.account_id == account.id,
                        WorkspaceMembership.membership_state.in_(("joined", "invited")),
                        Workspace.status == "active",
                        owner.proxy.is_not(None),
                        func.trim(owner.proxy) != "",
                    )
                    .order_by(
                        case((WorkspaceMembership.membership_state == "joined", 0), else_=1),
                        Workspace.id,
                    )
                )
            ).scalars().first()
            proxy_origin = "workspace_owner"
        if proxy_account is None:
            proxy_account = account
            proxy_origin = "account"

        mailbox = mailbox_readiness_snapshot(account)
        pickup_url = parse_mail_line(account.mail_raw or "").get("pickup_url") or ""
        cf_config = await load_cf_config(db)
        if pickup_url:
            mailbox_route = "pickup"
        elif mailbox["ready"]:
            mailbox_route = "hme"
        elif cf_config["admin_password"]:
            mailbox_route = "cloudflare"
        else:
            mailbox_route = "missing"
        return {
            "proxy": {
                "url": str(proxy_account.proxy or "").strip(),
                "source": proxy_account.proxy_source or ("workspace_owner" if proxy_origin == "workspace_owner" else "legacy"),
                "origin": proxy_origin,
                "account_id": proxy_account.id if proxy_origin == "workspace_owner" else account.id,
                "remote_id": proxy_account.sub2api_proxy_id,
            },
            "mailbox": {
                **mailbox,
                "effective_ready": mailbox_route != "missing",
                "route": mailbox_route,
            },
            "pickup_url": pickup_url,
        }

    async def load_settings(self, db: AsyncSession) -> dict[str, Any]:
        env = load_settings()
        enabled_raw = await get_setting_value(
            db,
            "auto_reauth_enabled",
            str(DEFAULT_AUTO_REAUTH_ENABLED).lower(),
        )
        interval_raw = await get_setting_value(
            db, "auto_reauth_interval_minutes", str(DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES)
        )
        requested = as_bool(enabled_raw, DEFAULT_AUTO_REAUTH_ENABLED)
        deployment_allowed = bool(env.auto_reauth_enabled)
        effective = requested and deployment_allowed
        return {
            "enabled": effective,
            "requested": requested,
            "deployment_allowed": deployment_allowed,
            "effective": effective,
            "blocked_reasons": [] if effective else (["deployment_disabled"] if requested else ["not_requested"]),
            "interval_minutes": clamp_auto_reauth_interval_minutes(interval_raw),
        }

    async def mark_outcome(
        self,
        db: AsyncSession,
        account: Account,
        *,
        success: bool,
        error_code: str = "",
        now: datetime | None = None,
        interval_minutes: int = DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES,
    ) -> None:
        stamp = now or utcnow()
        code = str(error_code or "")[:40]
        if success:
            account.reauth_fail_count = 0
            account.last_reauth_code = None
            account.next_reauth_at = stamp + timedelta(minutes=interval_minutes)
            set_auth_state(account, "healthy")
        else:
            account.reauth_fail_count = int(account.reauth_fail_count or 0) + 1
            account.last_reauth_code = code or "browser_failed"
            account.next_reauth_at = reauth_backoff_at(stamp, account.reauth_fail_count, error_code=code)
            if code == "account_deactivated":
                set_auth_state(account, "deactivated")
            elif code in {"identity_conflict", "owner_manual"}:
                set_auth_state(account, "manual_required")
            else:
                set_auth_state(account, "oauth_required")
        account.updated_at = stamp

    async def start_auto_reauth(
        self,
        db: AsyncSession,
        account: Account,
        *,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        stamp = now or utcnow()
        email = normalize_email(account.email)
        gate = await automation_gate(db, email=email)
        if not gate.get("allow"):
            await self.mark_outcome(db, account, success=False, error_code=str(gate.get("error_code") or "identity_unbound"), now=stamp)
            await db.commit()
            return {
                "success": False,
                "skipped": True,
                "error_code": gate.get("error_code") or "identity_unbound",
                "error": gate.get("reason") or "身份不准，停止自动重授权",
                "status": reauth_terminal_status(success=False, error_code=str(gate.get("error_code") or "")),
            }
        password = decrypt_secret(account.password_encrypted)
        context = await self.execution_context(db, account)
        pickup = context["pickup_url"]
        proxy = context["proxy"]["url"]
        plan = auto_reauth_plan(
            email=email,
            role="child",
            password=password,
            pickup_url=pickup,
            cf_ready=context["mailbox"]["effective_ready"],
            proxy=proxy,
        )
        if not plan.get("auto"):
            set_auth_state(account, "oauth_required")
            await db.commit()
            return {"success": False, "skipped": True, "error_code": "reauth_manual", "error": plan.get("reason")}
        active = await operation_store.active_for_email(db, email)
        if active:
            return {"success": True, "skipped": True, "job_id": active.public_id, "error_code": "already_running"}
        busy = await operation_store.browser_busy(db)
        if busy:
            return {"success": False, "skipped": True, "job_id": busy.public_id, "error_code": "browser_busy", "error": f"已有浏览器任务 {busy.email or busy.public_id}"}
        auth = chatgpt_client.create_oauth_authorize_url(
            client_id=oauth_sessions.CLIENT_ID or DEFAULT_OAUTH_CLIENT_ID,
            redirect_uri=oauth_sessions.REDIRECT_URI or OAUTH_REDIRECT_URI,
            login_hint=email,
        )
        session = oauth_sessions.create_session(
            team_id=int(account.source_team_id or 0),
            email=email,
            authorize=auth,
            role="child",
            mode="auto",
            proxy=proxy,
            proxy_source=context["proxy"]["source"],
            sub2api_proxy_id=context["proxy"]["remote_id"],
            password=password,
        )
        row = await operation_store.create(
            db,
            op_type="reauth",
            account_id=account.id,
            workspace_id=0,
            email=email,
            input_payload={"email": email, "ticket": session["ticket"], "proxy": proxy, "account_id": account.id},
            source="auto",
            state="queued",
        )
        frozen, profile_id = await proxy_profile_service.freeze(
            db,
            job_id=row.public_id,
            form_proxy=proxy,
            child_proxy=proxy,
            mother_proxy="",
        )
        if frozen:
            proxy = frozen
            oauth_sessions.mark_session(session["ticket"], proxy=proxy)
        oauth_sessions.mark_session(session["ticket"], job_id=row.public_id, status="queued", message=plan["reason"])
        await operation_store.note(db, row, "queued", plan["reason"], touch_lease=False)
        stored_session = oauth_sessions.get_session(session["ticket"])
        if stored_session is None:
            raise OAuthSessionError("无法保存 OAuth 会话", error_code="oauth_session_invalid")
        await oauth_session_store.persist(
            db,
            stored_session,
            purpose="account_reauth",
            account_id=account.id,
            credential_revision=int(account.credential_revision or 1),
        )
        await db.commit()
        return {
            "success": True,
            "job_id": row.public_id,
            "ticket": session["ticket"],
            "message": plan["reason"],
            "proxy_profile_id": profile_id,
        }

    async def run_immediate_reauth(
        self,
        db: AsyncSession,
        account: Account,
        *,
        now: datetime | None = None,
        progress_job_id: str | None = None,
        skip_browser_busy: bool = False,
    ) -> dict[str, Any]:
        """Run auto reauth in this request. Do not queue the disabled dispatcher."""
        stamp = now or utcnow()
        email = normalize_email(account.email)
        gate = await automation_gate(db, email=email)
        if not gate.get("allow"):
            await self.mark_outcome(db, account, success=False, error_code=str(gate.get("error_code") or "identity_unbound"), now=stamp)
            await db.commit()
            return {
                "success": False,
                "skipped": True,
                "error_code": gate.get("error_code") or "identity_unbound",
                "error": gate.get("reason") or "身份不准，停止自动重授权",
                "status": reauth_terminal_status(success=False, error_code=str(gate.get("error_code") or "")),
            }
        password = decrypt_secret(account.password_encrypted)
        context = await self.execution_context(db, account)
        pickup = context["pickup_url"]
        proxy = context["proxy"]["url"]
        plan = auto_reauth_plan(
            email=email,
            role="child",
            password=password,
            pickup_url=pickup,
            cf_ready=context["mailbox"]["effective_ready"],
            proxy=proxy,
        )
        if not plan.get("auto"):
            set_auth_state(account, "oauth_required")
            await db.commit()
            return {"success": False, "skipped": True, "error_code": "reauth_manual", "error": plan.get("reason")}
        if not skip_browser_busy:
            busy = await operation_store.browser_busy(db)
            if busy and busy.public_id != progress_job_id:
                return {
                    "success": False,
                    "skipped": True,
                    "job_id": busy.public_id,
                    "error_code": "browser_busy",
                    "error": f"已有浏览器任务 {busy.email or busy.public_id}",
                }
        auth = chatgpt_client.create_oauth_authorize_url(
            client_id=oauth_sessions.CLIENT_ID or DEFAULT_OAUTH_CLIENT_ID,
            redirect_uri=oauth_sessions.REDIRECT_URI or OAUTH_REDIRECT_URI,
            login_hint=email,
        )
        session = oauth_sessions.create_session(
            team_id=int(account.source_team_id or 0),
            email=email,
            authorize=auth,
            role="child",
            mode="auto",
            proxy=proxy,
            proxy_source=context["proxy"]["source"],
            sub2api_proxy_id=context["proxy"]["remote_id"],
            password=password,
        )
        if progress_job_id:
            frozen, _profile_id = await proxy_profile_service.freeze(
                db,
                job_id=progress_job_id,
                form_proxy=proxy,
                child_proxy=proxy,
                mother_proxy="",
            )
            if frozen:
                proxy = frozen
                oauth_sessions.mark_session(session["ticket"], proxy=proxy)
        oauth_sessions.mark_session(
            session["ticket"],
            job_id=progress_job_id or "",
            status="running",
            message=plan["reason"],
        )
        stored_session = oauth_sessions.get_session(session["ticket"])
        if stored_session is None:
            raise OAuthSessionError("无法保存 OAuth 会话", error_code="oauth_session_invalid")
        await oauth_session_store.persist(
            db,
            stored_session,
            purpose="account_reauth",
            account_id=account.id,
            credential_revision=int(account.credential_revision or 1),
        )
        await db.commit()
        ticket = session["ticket"]

        def on_stage(stage: str, message: str) -> None:
            oauth_sessions.mark_session(ticket, status="running", message=message)

        cf_config = await load_cf_config(db)
        hme_config = await load_hme_config(db)
        mailbox_ready = mailbox_readiness_snapshot(account)["ready"]
        use_cloudflare = (not pickup) and (not mailbox_ready) and bool(cf_config["admin_password"])
        browser = await browser_slot.run_reauth_isolated(
            email=account.email,
            password=password,
            authorize_url=str(session.get("authorize_url") or ""),
            proxy=proxy,
            pickup_url=pickup,
            phone=str(account.phone or ""),
            sms_url=str(account.sms_url or ""),
            use_cloudflare=use_cloudflare,
            cf_base_url=cf_config["base_url"],
            cf_address=cf_config["address"],
            cf_admin_password=cf_config["admin_password"],
            hme_base_url=hme_config.base_url if mailbox_ready else "",
            hme_service_token=hme_config.service_token if mailbox_ready else "",
            hme_account_id=str(account.hme_account_id or "") if mailbox_ready else "",
            on_stage=on_stage,
        )
        if not browser.get("ok"):
            code = str(browser.get("error_code") or "browser_failed")
            status = reauth_terminal_status(success=False, error_code=code)
            await self.mark_outcome(db, account, success=False, error_code=code)
            await db.commit()
            return {
                "success": False,
                "error": browser.get("error") or "自动授权失败",
                "error_code": code,
                "status": status,
            }
        try:
            stored, parsed = await oauth_session_store.begin_exchange(
                db,
                ticket,
                str(browser.get("callback_url") or ""),
                account_id=account.id,
                purpose="account_reauth",
            )
        except OAuthSessionError as exc:
            await db.commit()
            return {"success": False, "error": str(exc), "error_code": exc.error_code, "status": "manual_required"}
        await db.refresh(account)
        if stored.credential_revision != int(account.credential_revision or 1):
            await oauth_session_store.finish(db, stored, success=False)
            await db.commit()
            return {
                "success": False,
                "error": "凭证版本已变化",
                "error_code": "credential_revision_conflict",
                "status": "manual_required",
            }
        exchange_context = oauth_session_store.exchange_context(stored)
        exchanged = await chatgpt_client.exchange_oauth_code(
            code=parsed["code"],
            client_id=exchange_context["client_id"] or DEFAULT_OAUTH_CLIENT_ID,
            redirect_uri=exchange_context["redirect_uri"] or OAUTH_REDIRECT_URI,
            code_verifier=exchange_context["code_verifier"],
            db_session=db,
            identifier=account.email,
        )
        if not exchanged.get("success"):
            await oauth_session_store.finish(db, stored, success=False)
            code = str(exchanged.get("error_code") or "oauth_exchange_failed")
            status = reauth_terminal_status(success=False, error_code=code)
            await self.mark_outcome(db, account, success=False, error_code=code)
            await db.commit()
            return {
                "success": False,
                "error": exchanged.get("error") or "换票失败",
                "error_code": code,
                "status": status,
            }
        await auth_service.apply_tokens(account, exchanged)
        await oauth_session_store.finish(db, stored, success=True)
        oauth_sessions.pop_session(ticket)
        await self.mark_outcome(db, account, success=True)
        await db.commit()
        from app.application.quota import quota_service
        await quota_service.enqueue_after_credentials(db, account)
        return {"success": True, "status": "success", "ticket": ticket, "message": "reauth complete"}

    async def start_manual_reauth(self, db: AsyncSession, account: Account) -> dict[str, Any]:
        email = normalize_email(account.email)
        role = "owner" if str(account.local_purpose or "") == "mother" else "child"
        auth = chatgpt_client.create_oauth_authorize_url(
            client_id=oauth_sessions.CLIENT_ID or DEFAULT_OAUTH_CLIENT_ID,
            redirect_uri=oauth_sessions.REDIRECT_URI or OAUTH_REDIRECT_URI,
            login_hint=email,
        )
        session = oauth_sessions.create_session(
            team_id=int(account.source_team_id or 0),
            email=email,
            authorize=auth,
            role=role,
            mode="manual",
            proxy=str(account.proxy or ""),
            password="",
        )
        oauth_sessions.mark_session(
            session["ticket"],
            account_id=account.id,
            original_auth_state=account.auth_state,
            status="waiting",
            message="等待粘贴回调",
        )
        stored_session = oauth_sessions.get_session(session["ticket"])
        if stored_session is None:
            raise OAuthSessionError("无法保存 OAuth 会话", error_code="oauth_session_invalid")
        await oauth_session_store.persist(
            db,
            stored_session,
            purpose="account_reauth",
            account_id=account.id,
            credential_revision=int(account.credential_revision or 1),
        )
        await db.commit()
        return {
            "ok": True,
            "success": True,
            "account_id": account.id,
            "email": email,
            "ticket": session["ticket"],
            "authorize_url": session.get("authorize_url") or auth.get("authorize_url") or "",
            "redirect_uri": session.get("redirect_uri") or oauth_sessions.REDIRECT_URI,
            "message": "打开授权链接，登录后把回调地址贴回来",
            **build_auth_status(account),
        }

    async def complete_manual_reauth(
        self,
        db: AsyncSession,
        account: Account,
        *,
        ticket: str,
        callback_url: str,
        client=None,
    ) -> dict[str, Any]:
        from app.core.jwt import jwt_parser

        try:
            stored, parsed = await oauth_session_store.begin_exchange(
                db,
                ticket,
                callback_url,
                account_id=account.id,
                purpose="account_reauth",
            )
        except OAuthSessionError as exc:
            return {"ok": False, "success": False, "error": str(exc), "error_code": exc.error_code}
        await db.refresh(account)
        if stored.credential_revision != int(account.credential_revision or 1):
            await oauth_session_store.finish(db, stored, success=False)
            await db.commit()
            return {
                "ok": False,
                "success": False,
                "error": "凭证已被另一个流程更新，本次旧授权结果不会覆盖",
                "error_code": "credential_revision_conflict",
            }
        context = oauth_session_store.exchange_context(stored)
        exchanger = client or chatgpt_client
        exchanged = await exchanger.exchange_oauth_code(
            code=parsed["code"],
            client_id=context["client_id"] or DEFAULT_OAUTH_CLIENT_ID,
            redirect_uri=context["redirect_uri"] or OAUTH_REDIRECT_URI,
            code_verifier=context["code_verifier"],
            db_session=db,
            identifier=account.email,
        )
        if not exchanged.get("success") or not exchanged.get("access_token"):
            await oauth_session_store.finish(db, stored, success=False)
            await db.commit()
            return {
                "ok": False,
                "success": False,
                "error": str(exchanged.get("error") or "换票失败"),
                "error_code": str(exchanged.get("error_code") or "oauth_exchange_failed"),
            }
        token_email = jwt_parser.extract_email(str(exchanged.get("access_token") or ""))
        if token_email and token_email != normalize_email(account.email):
            await oauth_session_store.finish(db, stored, success=False)
            await db.commit()
            return {
                "ok": False,
                "success": False,
                "error": f"登录邮箱是 {token_email}，和账号 {account.email} 不一致",
                "error_code": "token_identity_mismatch",
            }
        await auth_service.apply_tokens(account, exchanged)
        await self.mark_outcome(db, account, success=True)
        await oauth_session_store.finish(db, stored, success=True)
        oauth_sessions.pop_session(ticket)
        await db.commit()
        from app.application.quota import quota_service
        await quota_service.enqueue_after_credentials(db, account)
        token_sync = await push_refreshed_tokens_to_bound_sub2api(db, account)
        await db.commit()
        return {
            "ok": True,
            "success": True,
            "account_id": account.id,
            "email": account.email,
            "auth_state": account.auth_state,
            "credential_revision": account.credential_revision,
            "sub2api_token_sync": token_sync,
            "message": f"{account.email} 授权已更新",
        }

    async def run_job(self, db: AsyncSession, public_id: str, ticket: str) -> dict[str, Any]:

        row = await operation_store.get_by_public_id(db, public_id)
        session = oauth_sessions.get_session(ticket) or await oauth_session_store.runtime_session(db, ticket)
        if row is None or session is None:
            if row is not None:
                await operation_store.finish(
                    db,
                    row,
                    {"success": False, "error": "认证会话不存在或已过期", "error_code": "oauth_expired", "status": "manual_required"},
                )
                await db.commit()
            return {"success": False, "error_code": "oauth_expired"}
        account = await db.get(Account, row.account_id) if row.account_id else None
        if account is None:
            email = str(session.get("email") or row.email or "")
            account = (await db.execute(select(Account).where(Account.email == normalize_email(email)))).scalar_one_or_none()
        if account is None:
            await operation_store.finish(db, row, {"success": False, "error": "本地账号不存在", "error_code": "identity_unbound", "status": "manual_required"})
            await db.commit()
            return {"success": False, "error_code": "identity_unbound"}
        payload = unpack_input(row.input_json)
        frozen = await proxy_profile_service.frozen_url(db, row.public_id)
        proxy = frozen or str(payload.get("proxy") or account.proxy or session.get("proxy") or "")
        password = decrypt_secret(account.password_encrypted) or str(session.get("login_password") or "")
        pickup = parse_mail_line(account.mail_raw or "").get("pickup_url") or ""
        cf_config = await load_cf_config(db)
        hme_config = await load_hme_config(db)
        mailbox_ready = mailbox_readiness_snapshot(account)["ready"]
        use_cloudflare = (not pickup) and (not mailbox_ready) and bool(cf_config["admin_password"])

        def on_stage(stage: str, message: str) -> None:
            oauth_sessions.mark_session(ticket, status="running", message=message)

        await operation_store.note(db, row, "browser", "正在自动登录并完成授权")
        await db.commit()
        browser = await browser_slot.run_reauth_isolated(
            email=account.email,
            password=password,
            authorize_url=str(session.get("authorize_url") or ""),
            proxy=proxy,
            pickup_url=pickup,
            phone=str(account.phone or ""),
            sms_url=str(account.sms_url or ""),
            use_cloudflare=use_cloudflare,
            cf_base_url=cf_config["base_url"],
            cf_address=cf_config["address"],
            cf_admin_password=cf_config["admin_password"],
            hme_base_url=hme_config.base_url if mailbox_ready else "",
            hme_service_token=hme_config.service_token if mailbox_ready else "",
            hme_account_id=str(account.hme_account_id or "") if mailbox_ready else "",
            on_stage=on_stage,
        )
        if not browser.get("ok"):
            code = str(browser.get("error_code") or "browser_failed")
            status = reauth_terminal_status(success=False, error_code=code)
            await operation_store.finish(
                db,
                row,
                {"success": False, "error": browser.get("error") or "自动授权失败", "error_code": code, "status": status},
            )
            await self.mark_outcome(db, account, success=False, error_code=code)
            await db.commit()
            return {"success": False, "error_code": code, "status": status}
        await db.refresh(row)
        if row.cancel_requested:
            await operation_store.finish(
                db,
                row,
                {"success": False, "status": "cancelled", "error_code": "cancelled", "error": "operation cancelled before credential exchange"},
            )
            await db.commit()
            return {"success": False, "error_code": "cancelled", "status": "cancelled"}
        try:
            stored, parsed = await oauth_session_store.begin_exchange(
                db,
                ticket,
                str(browser.get("callback_url") or ""),
                account_id=account.id,
                purpose="account_reauth",
            )
        except OAuthSessionError as exc:
            await operation_store.finish(
                db,
                row,
                {"success": False, "error": str(exc), "error_code": exc.error_code, "status": "manual_required"},
            )
            await db.commit()
            return {"success": False, "error_code": exc.error_code, "status": "manual_required"}
        await db.refresh(account)
        if stored.credential_revision != int(account.credential_revision or 1):
            await oauth_session_store.finish(db, stored, success=False)
            await operation_store.finish(
                db,
                row,
                {"success": False, "error": "凭证版本已变化", "error_code": "credential_revision_conflict", "status": "manual_required"},
            )
            await db.commit()
            return {"success": False, "error_code": "credential_revision_conflict", "status": "manual_required"}
        context = oauth_session_store.exchange_context(stored)
        exchanged = await chatgpt_client.exchange_oauth_code(
            code=parsed["code"],
            client_id=context["client_id"] or DEFAULT_OAUTH_CLIENT_ID,
            redirect_uri=context["redirect_uri"] or OAUTH_REDIRECT_URI,
            code_verifier=context["code_verifier"],
            db_session=db,
            identifier=account.email,
        )
        if not exchanged.get("success"):
            await oauth_session_store.finish(db, stored, success=False)
            code = str(exchanged.get("error_code") or "oauth_exchange_failed")
            status = reauth_terminal_status(success=False, error_code=code)
            await operation_store.finish(
                db,
                row,
                {"success": False, "error": exchanged.get("error") or "换票失败", "error_code": code, "status": status},
            )
            await self.mark_outcome(db, account, success=False, error_code=code)
            await db.commit()
            return {"success": False, "error_code": code, "status": status}
        await auth_service.apply_tokens(account, exchanged)
        await oauth_session_store.finish(db, stored, success=True)
        oauth_sessions.pop_session(ticket)
        await self.mark_outcome(db, account, success=True)
        await db.commit()
        from app.application.quota import quota_service
        await quota_service.enqueue_after_credentials(db, account)
        token_sync = await push_refreshed_tokens_to_bound_sub2api(db, account, operation=row)
        await operation_store.finish(
            db,
            row,
            {
                "success": True,
                "status": "success",
                "message": "reauth complete",
                "sub2api_token_sync": token_sync,
            },
        )
        await db.commit()
        return {"success": True, "status": "success", "sub2api_token_sync": token_sync}

    async def run_once(
        self,
        db: AsyncSession,
        *,
        now: datetime | None = None,
        settings: dict[str, Any] | None = None,
        execute: bool = False,
    ) -> dict[str, Any]:
        cfg = settings or await self.load_settings(db)
        stamp = now or utcnow()
        stats = {"enabled": bool(cfg.get("enabled")), "scanned": 0, "queued": 0, "skipped": 0, "failed": 0, "deactivated": 0, "conflict": 0, "email": ""}
        if not cfg.get("enabled"):
            stats["skipped"] = 1
            return stats
        busy = await operation_store.browser_busy(db)
        if busy:
            stats["skipped"] = 1
            stats["email"] = busy.email or ""
            return stats
        accounts = list(
            (
                await db.execute(
                    select(Account).where(
                        Account.local_purpose.in_(("child", "standby")),
                        or_(Account.next_reauth_at.is_(None), Account.next_reauth_at <= stamp),
                    )
                )
            ).scalars()
        )
        candidates: list[Account] = []
        for account in accounts:
            stats["scanned"] += 1
            if not account.auto_reauth_opt_in:
                stats["skipped"] += 1
                continue
            if str(account.operational_state or "") in SKIP_OPERATIONAL_STATES | {"standby", "free", "unused"}:
                stats["skipped"] += 1
                continue
            if str(account.last_reauth_code or "") in {"account_deactivated", "identity_conflict"}:
                stats["skipped"] += 1
                continue
            if str(account.auth_state or "") not in {"oauth_required", "refresh_due", "unknown", "phone_required"}:
                if decrypt_secret(account.access_token_encrypted):
                    stats["skipped"] += 1
                    continue
            gate = await automation_gate(db, email=account.email)
            if not gate.get("allow"):
                stats["skipped"] += 1
                if gate.get("error_code") == "identity_conflict":
                    stats["conflict"] += 1
                    await self.mark_outcome(db, account, success=False, error_code="identity_conflict", now=stamp)
                continue
            context = await self.execution_context(db, account)
            plan = auto_reauth_plan(
                email=account.email,
                role="child",
                password=decrypt_secret(account.password_encrypted),
                pickup_url=context["pickup_url"],
                cf_ready=context["mailbox"]["effective_ready"],
                proxy=context["proxy"]["url"],
            )
            if not plan.get("auto"):
                stats["skipped"] += 1
                continue
            if await operation_store.active_for_email(db, account.email):
                stats["skipped"] += 1
                continue
            candidates.append(account)
        if not candidates:
            await db.commit()
            return stats
        account = sorted(candidates, key=lambda item: (item.next_reauth_at or stamp, item.id))[0]
        result = await self.start_auto_reauth(db, account, now=stamp)
        stats["email"] = account.email
        if result.get("skipped") and result.get("error_code") in {"already_running", "browser_busy"}:
            stats["skipped"] += 1
            return stats
        if result.get("success") and not result.get("skipped"):
            stats["queued"] = 1
            if execute and result.get("job_id") and result.get("ticket"):
                await self.run_job(db, result["job_id"], result["ticket"])
        else:
            code = str(result.get("error_code") or "browser_failed")
            if code == "account_deactivated":
                stats["deactivated"] = 1
            elif code == "identity_conflict":
                stats["conflict"] += 1
            else:
                stats["failed"] += 1
        await db.commit()
        return stats


reauth_service = ReauthService()
