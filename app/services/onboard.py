"""子号拉人引擎：邀请 -> 注册/复用登录 -> 接码 -> 对账 -> 推 Sub2API。"""
from __future__ import annotations

import asyncio
import logging
import secrets
import string
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import ChildAccount, Team, TeamEmailMapping
from app.services.child_accounts import (
    ACTIVE_CHILD_STATUSES,
    CHILD_STATUS_ACTIVE,
    child_account_service,
    normalize_email,
)
from app.services.mail_otp import mail_otp_client, parse_mail_line
from app.services.sms import parse_phone_line, require_proxy
from app.services.sub2api import sub2api_service
from app.services.team import team_service
from app.utils.proxy import normalize_proxy_url
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


def _random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12)) + "Aa1!"


class OnboardService:
    async def _load_team(self, db_session: AsyncSession, team_id: int) -> Team:
        stmt = select(Team).where(Team.id == team_id).options(selectinload(Team.email_mappings))
        result = await db_session.execute(stmt)
        team = result.scalar_one_or_none()
        if not team:
            raise ValueError(f"Team {team_id} 不存在")
        return team

    def _child_proxy(self, child: ChildAccount, team: Team) -> str:
        return require_proxy(child.proxy or team.proxy, "子号浏览器/接码")

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

    async def _run_browser(
        self,
        *,
        email: str,
        password: str,
        pickup_url: str,
        phone: str,
        sms_url: str,
        proxy: str,
        start_url: str = "",
        mode: str = "register",
    ) -> Dict[str, Any]:
        from app.services.browser_onboard import run_browser_onboard

        return run_browser_onboard(
            email=email,
            password=password,
            pickup_url=pickup_url,
            phone=phone,
            sms_url=sms_url,
            proxy=proxy,
            start_url=start_url,
            mode=mode,
        )

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
    ) -> Dict[str, Any]:
        parsed = parse_mail_line(email_line)
        email = normalize_email(parsed["email"] or email_line)
        if not email or "@" not in email:
            raise ValueError("请输入有效邮箱")

        team = await self._load_team(db_session, team_id)
        self._team_proxy(team)
        child_proxy = proxy or ""
        existing = await child_account_service.get_by_email(db_session, email)
        if existing and existing.status in ACTIVE_CHILD_STATUSES and existing.current_team_id == team.id:
            return {"success": True, "status": "already_exists", "message": f"{email} 已在该 Team 中", "child": child_account_service.serialize(existing)}

        phone, sms_url = parse_phone_line(phone_line)
        if existing:
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
            password=password or _random_password(),
            mail_raw=parsed.get("raw") or email_line,
            phone=phone,
            sms_url=sms_url,
            proxy=child_proxy,
            cycle_days=team.seat_cycle_days or 7,
        )
        password = password or child_account_service.decrypt_secret(child.password_encrypted) or _random_password()
        if not child.password_encrypted:
            child.password_encrypted = child_account_service.encrypt_secret(password)

        invite = await team_service.add_team_member(team_id, email, db_session)
        if not invite.get("success"):
            await child_account_service.record_event(
                db_session,
                email=email,
                action="invite",
                team_id=team.id,
                child_id=child.id,
                success=False,
                detail=invite.get("error") or "邀请失败",
            )
            await db_session.commit()
            return {"success": False, "error": invite.get("error") or "邀请失败"}

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
        await db_session.commit()

        has_session = bool(child_account_service.decrypt_secret(child.access_token_encrypted) or child_account_service.decrypt_secret(child.session_token_encrypted))
        should_register = not (reuse_existing and has_session and child_account_service.decrypt_secret(child.password_encrypted))
        pickup_url = parsed.get("pickup_url") or ""
        if not pickup_url and child.mail_raw:
            pickup_url = parse_mail_line(child.mail_raw).get("pickup_url") or ""

        invite_url = ""
        if pickup_url:
            try:
                invite_url = await asyncio.to_thread(
                    mail_otp_client.wait_for_invite,
                    pickup_url,
                    proxy=self._child_proxy(child, team),
                    email=email,
                    timeout_sec=90,
                ) or ""
            except Exception as exc:  # noqa: BLE001
                logger.warning("等待邀请邮件失败: %s", exc)

        browser_mode = "register" if should_register else "relogin"
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
            )
        except Exception as exc:  # noqa: BLE001
            child.last_error = str(exc)
            await child_account_service.record_event(
                db_session,
                email=email,
                action=browser_mode,
                team_id=team.id,
                child_id=child.id,
                success=False,
                detail=str(exc),
            )
            await db_session.commit()
            return {"success": False, "error": str(exc), "status": "browser_failed"}

        if not browser_result.get("ok"):
            child.last_error = browser_result.get("error") or "浏览器流程失败"
            await child_account_service.record_event(
                db_session,
                email=email,
                action=browser_mode,
                team_id=team.id,
                child_id=child.id,
                success=False,
                detail=child.last_error,
            )
            await db_session.commit()
            return {"success": False, "error": child.last_error, "status": "browser_failed"}

        await child_account_service.save_tokens(db_session, child, {
            "access_token": browser_result.get("access_token") or "",
            "refresh_token": browser_result.get("refresh_token") or "",
            "session_token": browser_result.get("session_token") or "",
            "id_token": browser_result.get("id_token") or "",
            "account_id": browser_result.get("account_id") or "",
            "client_id": browser_result.get("client_id") or "",
        })
        if browser_result.get("password"):
            child.password_encrypted = child_account_service.encrypt_secret(browser_result["password"])

        joined = False
        last_error = ""
        for _ in range(8):
            try:
                joined = await self._confirm_joined(db_session, team.id, email)
                if joined:
                    break
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
            await asyncio.sleep(5)

        if not joined:
            detail = last_error or "邀请后对账未看到该成员，未推送 Sub2API"
            child.last_error = detail
            await child_account_service.record_event(
                db_session,
                email=email,
                action="reconcile",
                team_id=team.id,
                child_id=child.id,
                success=False,
                detail=detail,
            )
            await db_session.commit()
            return {"success": False, "error": detail, "status": "not_joined"}

        mapping = await self._mapping(db_session, team.id, email)
        await child_account_service.mark_active(db_session, child, team, mapping=mapping)

        push_result = None
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
            )
            if push_result.get("account_id"):
                child.sub2api_account_id = int(push_result["account_id"])
            await child_account_service.record_event(
                db_session,
                email=email,
                action="push",
                team_id=team.id,
                child_id=child.id,
                success=True,
                detail=push_result.get("strategy") or "pushed",
            )
        except Exception as exc:  # noqa: BLE001
            child.last_error = str(exc)
            await child_account_service.record_event(
                db_session,
                email=email,
                action="push",
                team_id=team.id,
                child_id=child.id,
                success=False,
                detail=str(exc),
            )
            await db_session.commit()
            return {
                "success": False,
                "error": f"已入组，但推送 Sub2API 失败: {exc}",
                "status": "push_failed",
                "child": child_account_service.serialize(child),
            }

        await db_session.commit()
        return {
            "success": True,
            "status": "active",
            "message": f"{email} 已入组并推送到 Sub2API",
            "child": child_account_service.serialize(child),
            "push": push_result,
        }

    async def kick_to_standby(
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

        if not user_id:
            members = await team_service.get_team_members(team_id, db_session)
            if members.get("success"):
                for item in members.get("members") or []:
                    if normalize_email(item.get("email")) == target and item.get("user_id"):
                        user_id = item.get("user_id")
                        break

        if user_id:
            result = await team_service.delete_team_member(team_id, user_id, db_session, email=target)
        else:
            result = await team_service.revoke_team_invite(team_id, target, db_session)

        if not result.get("success"):
            await child_account_service.record_event(
                db_session,
                email=target,
                action="kick",
                team_id=team.id,
                child_id=child.id if child else None,
                success=False,
                detail=result.get("error") or "踢人失败",
            )
            await db_session.commit()
            return {"success": False, "error": result.get("error") or "踢人失败"}

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
        return {
            "success": True,
            "message": f"{target} 已踢出，子号进入 standby",
            "child": child_account_service.serialize(child) if child else None,
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
    ) -> Dict[str, Any]:
        team = await self._load_team(db_session, team_id)
        due = await child_account_service.list_due_accounts(db_session, team_id=team.id)
        if not due:
            return {"success": False, "error": "该 Team 没有到期需要踢出的子号"}

        kick_target = due[0]
        kick_result = await self.kick_to_standby(db_session, team_id=team.id, email=kick_target.email)
        if not kick_result.get("success"):
            return kick_result

        replacement = None
        if child_id:
            replacement = await child_account_service.get_by_id(db_session, child_id)
        elif email_line:
            replacement_email = normalize_email(parse_mail_line(email_line)["email"] or email_line)
            replacement = await child_account_service.get_by_email(db_session, replacement_email)

        if replacement is None and not email_line:
            standby = await child_account_service.list_accounts(db_session, status="standby")
            for item in standby:
                if item.email != kick_target.email:
                    replacement = item
                    break

        if replacement is None and not email_line:
            return {
                "success": False,
                "error": f"已踢出 {kick_target.email}，但没有可复用子号，也没有提供新邮箱",
                "kick": kick_result,
            }

        invite_line = email_line or (replacement.mail_raw if replacement else replacement.email)
        invite_result = await self.invite_and_onboard(
            db_session,
            team_id=team.id,
            email_line=invite_line,
            phone_line=phone_line or ((replacement.phone or "") + ("----" + replacement.sms_url if replacement and replacement.sms_url else "")),
            proxy=proxy or (replacement.proxy if replacement else ""),
            reuse_existing=True,
        )
        if not invite_result.get("success"):
            return {
                "success": False,
                "error": invite_result.get("error") or "补位失败，未完成轮转",
                "kick": kick_result,
                "invite": invite_result,
            }
        await child_account_service.record_event(
            db_session,
            email=invite_result.get("child", {}).get("email") or "",
            action="rotate",
            team_id=team.id,
            child_id=(invite_result.get("child") or {}).get("id"),
            success=True,
            detail=f"kicked {kick_target.email}",
        )
        await db_session.commit()
        return {
            "success": True,
            "message": f"已踢出 {kick_target.email} 并补入 {(invite_result.get('child') or {}).get('email')}",
            "kick": kick_result,
            "invite": invite_result,
        }


onboard_service = OnboardService()
