"""Workspace membership mutations. Kick is not delete; vacancy receipts stay evidence."""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.application.tokens import auth_service, decrypt_secret
from app.application.vacancy import vacancy_service
from app.core.time import utcnow
from app.domain.identity import (
    LOCAL_PURPOSE_STANDBY,
    MEMBERSHIP_STATE_REMOVED,
)
from app.domain.identity.ids import normalize_email
from app.domain.vacancy import parse_policy_notice, present_vacancy, summarize_for_message
from app.integrations.openai.chatgpt import chatgpt_client
from app.integrations.openai.member_adapter import (
    adapt_collection,
    is_owner_role,
    normalize_official_role,
    official_roles_equivalent,
    parse_invite_role,
    validate_fetch_counts,
)
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership

logger = logging.getLogger(__name__)


def workspace_error_is_fatal(result: dict[str, Any]) -> bool:
    error_code = str(result.get("error_code") or "").strip()
    error_msg = str(result.get("error") or "").lower()
    ban_codes = {
        "account_deactivated",
        "token_invalidated",
        "account_suspended",
        "account_not_found",
        "user_not_found",
        "deactivated_workspace",
    }
    if error_code in ban_codes:
        return True
    ban_keywords = (
        "token has been invalidated",
        "account_deactivated",
        "account has been deactivated",
        "account is deactivated",
        "account_suspended",
        "account is suspended",
        "account was deleted",
        "user_not_found",
        "this account is deactivated",
        "deactivated_workspace",
    )
    return any(keyword in error_msg for keyword in ban_keywords)


def is_access_token_error(result: dict[str, Any]) -> bool:
    error_code = str(result.get("error_code") or "").strip().lower()
    error_msg = str(result.get("error") or "").lower()
    try:
        status_code = int(result.get("status_code") or 0)
    except (TypeError, ValueError):
        status_code = 0
    if error_code in {"token_expired", "unauthorized"}:
        return True
    if status_code == 401 and error_code != "token_invalidated":
        return True
    return any(marker in error_msg for marker in ("token_expired", "token is expired", "unauthorized"))


def remote_delete_succeeded(result: dict[str, Any]) -> bool:
    if result.get("success"):
        return True
    return chatgpt_client.is_already_removed_error(
        result.get("status_code"),
        result.get("error"),
        result.get("error_code"),
    )


def _delete_member_success(message: str, vacancy: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
    summary = summarize_for_message(vacancy)
    if summary:
        message = f"{message}。{summary}"
    payload: dict[str, Any] = {
        "success": True,
        "message": message,
        "error": None,
        "vacancy": vacancy,
    }
    payload.update(extra)
    return payload


class WorkspaceService:
    def __init__(self, client=None):
        self.client = client or chatgpt_client

    async def load_workspace(self, db: AsyncSession, workspace_id: int) -> Workspace | None:
        return (
            await db.execute(
                select(Workspace)
                .options(selectinload(Workspace.owner_account), selectinload(Workspace.memberships))
                .where(Workspace.id == int(workspace_id))
            )
        ).scalar_one_or_none()

    async def owner_account(self, db: AsyncSession, workspace: Workspace) -> Account | None:
        if workspace.owner_account is not None:
            return workspace.owner_account
        if workspace.owner_account_id:
            return await db.get(Account, workspace.owner_account_id)
        return None

    async def ensure_access_token(
        self,
        db: AsyncSession,
        workspace: Workspace,
        *,
        force_refresh: bool = False,
    ) -> str | None:
        owner = await self.owner_account(db, workspace)
        if owner is None:
            return None
        token = decrypt_secret(owner.access_token_encrypted)
        if token and not force_refresh:
            return token
        refreshed = await auth_service.refresh_account(db, owner)
        if refreshed.get("success"):
            return decrypt_secret(owner.access_token_encrypted)
        return decrypt_secret(owner.access_token_encrypted) or None

    def _workspace_account_id(self, workspace: Workspace) -> str:
        return str(workspace.official_workspace_id or "").strip()

    def _last_owner_guard(
        self,
        workspace: Workspace,
        owner: Account | None,
        live: dict[str, Any],
        live_item: dict[str, Any] | None,
        *,
        email: str,
    ) -> dict[str, Any] | None:
        target = normalize_email(email)
        if owner is not None and normalize_email(owner.email) == target:
            return {
                "success": False,
                "error": "不能踢出当前工作区的主控母号",
                "error_code": "primary_mother_protected",
            }
        lookup_state = live.get("lookup_state") or ("found" if live_item else ("absent_confirmed" if live.get("success") else "unknown_due_to_error"))
        if lookup_state == "unknown_due_to_error" or live.get("success") is False:
            return {
                "success": False,
                "error": live.get("error") or "官方成员列表读取失败，未执行删除",
                "error_code": live.get("error_code") or "kick_lookup_unknown",
            }
        if not live_item or live_item.get("status") == "invited":
            return None
        if not is_owner_role(live_item.get("role")):
            return None
        adapted = adapt_collection(live.get("members") or live.get("items") or [], default_state="joined")
        other_owners = [
            item
            for item in adapted["members"]
            if is_owner_role(item.get("role")) and item.get("email") != target and item.get("state") != "invited"
        ]
        if other_owners:
            return None
        return {
            "success": False,
            "error": "不能踢出最后一个官方 Owner",
            "error_code": "last_official_owner",
        }

    async def get_members(self, db: AsyncSession, workspace: Workspace) -> dict[str, Any]:
        token = await self.ensure_access_token(db, workspace)
        owner = await self.owner_account(db, workspace)
        account_id = self._workspace_account_id(workspace)
        if not token or not account_id:
            return {"success": False, "members": [], "error": "workspace token or official id missing", "error_code": "workspace_unconfigured"}
        return await self.client.get_members(
            token,
            account_id,
            db,
            identifier=owner.email if owner else "default",
        )

    async def get_invites(self, db: AsyncSession, workspace: Workspace) -> dict[str, Any]:
        token = await self.ensure_access_token(db, workspace)
        owner = await self.owner_account(db, workspace)
        account_id = self._workspace_account_id(workspace)
        if not token or not account_id:
            return {"success": False, "items": [], "error": "workspace token or official id missing", "error_code": "workspace_unconfigured"}
        return await self.client.get_invites(
            token,
            account_id,
            db,
            identifier=owner.email if owner else "default",
        )

    async def lookup_live_member(
        self,
        db: AsyncSession,
        workspace: Workspace,
        email: str,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        target = normalize_email(email)
        members = await self.get_members(db, workspace)
        invites = await self.get_invites(db, workspace)
        members_adapted = adapt_collection(members.get("members") or members.get("items") or [], default_state="joined")
        invites_adapted = adapt_collection(invites.get("items") or invites.get("members") or [], default_state="invited")
        members_check = validate_fetch_counts(
            reported_total=members.get("reported_total") if members.get("reported_total") is not None else members.get("total"),
            raw_item_count=int(members.get("raw_item_count") or members_adapted["raw_item_count"]),
            parsed_item_count=members_adapted["parsed_item_count"],
            invalid_item_count=members_adapted["invalid_item_count"],
            incomplete=bool(members.get("incomplete")),
        )
        invites_check = validate_fetch_counts(
            reported_total=invites.get("reported_total") if invites.get("reported_total") is not None else invites.get("total"),
            raw_item_count=int(invites.get("raw_item_count") or invites_adapted["raw_item_count"]),
            parsed_item_count=invites_adapted["parsed_item_count"],
            invalid_item_count=invites_adapted["invalid_item_count"],
            incomplete=bool(invites.get("incomplete")),
        )
        members_ok = bool(members.get("success")) and bool(members_check.get("ok"))
        invites_ok = bool(invites.get("success")) and bool(invites_check.get("ok"))
        found = None
        for item in members_adapted["members"]:
            if item["email"] == target:
                found = {**item, "status": "joined"}
                break
        if found is None:
            for item in invites_adapted["members"]:
                if item["email"] == target:
                    found = {**item, "status": "invited"}
                    break
        known = found is not None or (members_ok and invites_ok)
        envelope = {
            **members,
            "invites": invites,
            "members_ok": members_ok,
            "invites_ok": invites_ok,
            "lookup_state": "found" if found else ("absent_confirmed" if members_ok and invites_ok else "unknown_due_to_error"),
            "success": bool(known),
            "error": None if known else (members.get("error") or invites.get("error") or members_check.get("error") or invites_check.get("error")),
            "error_code": None if known else (members.get("error_code") or invites.get("error_code") or members_check.get("error_code") or invites_check.get("error_code") or "lookup_unknown"),
        }
        return envelope, found

    async def mark_membership_removed(self, db: AsyncSession, workspace_id: int, email: str) -> WorkspaceMembership | None:
        target = normalize_email(email)
        account = (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none()
        if account is None:
            return None
        membership = (
            await db.execute(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            return None
        membership.membership_state = MEMBERSHIP_STATE_REMOVED
        membership.removed_at = utcnow()
        return membership

    async def mark_standby(
        self,
        db: AsyncSession,
        account: Account,
        *,
        next_eligible_at=None,
        unbind_sub2api: bool = False,
        remote_unbind_confirmed: bool = False,
        binding_error: str | None = None,
    ) -> None:
        account.operational_state = "standby"
        account.local_purpose = LOCAL_PURPOSE_STANDBY
        if next_eligible_at is not None:
            account.next_eligible_at = next_eligible_at
        if unbind_sub2api:
            from app.persistence.models.identity import ExternalBinding
            from app.domain.identity import PROVIDER_SUB2API

            binding = (
                await db.execute(
                    select(ExternalBinding).where(
                        ExternalBinding.provider == PROVIDER_SUB2API,
                        ExternalBinding.local_account_id == account.id,
                    )
                )
            ).scalar_one_or_none()
            if binding is not None:
                if remote_unbind_confirmed:
                    await db.delete(binding)
                else:
                    binding.binding_state = "error"
                    binding.last_error = (binding_error or "remote unbind not confirmed")[:500]
                    binding.updated_at = utcnow()
        account.updated_at = utcnow()

    async def delete_member(
        self,
        db: AsyncSession,
        workspace_id: int,
        user_id: str,
        *,
        email: str | None = None,
    ) -> dict[str, Any]:
        workspace = await self.load_workspace(db, workspace_id)
        if workspace is None:
            return {"success": False, "error": f"未找到 Workspace {workspace_id}", "error_code": "workspace_not_found"}
        owner = await self.owner_account(db, workspace)
        account_id = self._workspace_account_id(workspace)
        access_token = await self.ensure_access_token(db, workspace)
        if not access_token or not account_id:
            return {"success": False, "error": "该 Workspace 的登录凭证已过期，且自动刷新失败", "error_code": "token_refresh_failed"}

        delete_result = await self.client.delete_member(
            access_token,
            account_id,
            user_id,
            db,
            identifier=owner.email if owner else "default",
        )
        if not remote_delete_succeeded(delete_result) and is_access_token_error(delete_result):
            refreshed = await self.ensure_access_token(db, workspace, force_refresh=True)
            if refreshed:
                delete_result = await self.client.delete_member(
                    refreshed,
                    account_id,
                    user_id,
                    db,
                    identifier=owner.email if owner else "default",
                )
        if not remote_delete_succeeded(delete_result):
            error_msg = delete_result.get("error") or "删除成员失败"
            if workspace_error_is_fatal(delete_result) and delete_result.get("error_code") == "account_deactivated":
                error_msg = "账号已封禁 (account_deactivated)"
            return {
                "success": False,
                "error": error_msg,
                "error_code": delete_result.get("error_code") or "kick_failed",
                "status_code": delete_result.get("status_code"),
            }

        vacancy = delete_result.get("vacancy") or parse_policy_notice(delete_result.get("data"))
        if vacancy:
            try:
                vacancy = await vacancy_service.record(
                    db,
                    workspace_id=workspace.id,
                    account_id=account_id,
                    user_id=user_id,
                    email=email,
                    vacancy=vacancy,
                )
            except Exception:
                logger.exception("保存席位阈值失败")
                vacancy = present_vacancy(vacancy)
        # The caller reconciles membership only after readback confirms departure.
        await db.flush()
        return _delete_member_success("成员已删除", vacancy, already_removed=bool(delete_result.get("already_removed")))

    async def update_member_role(
        self,
        db: AsyncSession,
        workspace_id: int,
        email: str,
        role: str = "owner",
        *,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        workspace = await self.load_workspace(db, workspace_id)
        if workspace is None:
            return {"success": False, "error": f"未找到 Workspace {workspace_id}", "error_code": "workspace_not_found"}
        try:
            requested_role = parse_invite_role(role)
        except ValueError:
            return {"success": False, "error": "官方角色只能是 Owner 或 Member", "error_code": "invalid_invite_role"}
        owner = await self.owner_account(db, workspace)
        account_id = self._workspace_account_id(workspace)
        access_token = await self.ensure_access_token(db, workspace)
        target = normalize_email(email)
        if not access_token or not account_id:
            return {"success": False, "error": "该 Workspace 的登录凭证已过期，且自动刷新失败", "error_code": "token_refresh_failed"}
        live, live_item = await self.lookup_live_member(db, workspace, target)
        if owner is not None and normalize_email(owner.email) == target:
            return {"success": False, "error": "不能改当前工作区的主控母号角色", "error_code": "primary_mother_protected"}
        if not live_item or live_item.get("status") != "joined":
            if live.get("lookup_state") == "unknown_due_to_error" or live.get("success") is False:
                return {
                    "success": False,
                    "error": live.get("error") or "官方成员列表读取失败，未改角色",
                    "error_code": live.get("error_code") or "kick_lookup_unknown",
                }
            return {
                "success": False,
                "error": f"{target} 还没加入官方席位，不能改已加入成员的角色",
                "error_code": "not_joined",
            }
        current_role = normalize_official_role(live_item.get("role"))
        if official_roles_equivalent(current_role, requested_role):
            return {
                "success": True,
                "already": True,
                "email": target,
                "role": requested_role,
                "existing_role": current_role,
                "message": f"{target} 官方角色已经是 {requested_role}",
            }
        if is_owner_role(current_role) and requested_role != "owner":
            guard = self._last_owner_guard(workspace, owner, live, live_item, email=target)
            if guard is not None:
                return {
                    "success": False,
                    "error": "不能把最后一个官方 Owner 改成 Member",
                    "error_code": "last_official_owner",
                }
        candidate = str(user_id or live_item.get("user_id") or "").strip()
        if not candidate:
            return {"success": False, "error": "官方成员缺少 user id，未改角色", "error_code": "missing_user_id"}
        result = await self.client.update_member_role(
            access_token,
            account_id,
            candidate,
            requested_role,
            db,
            identifier=owner.email if owner else "default",
        )
        if not result.get("success") and is_access_token_error(result):
            refreshed = await self.ensure_access_token(db, workspace, force_refresh=True)
            if refreshed:
                result = await self.client.update_member_role(
                    refreshed,
                    account_id,
                    candidate,
                    requested_role,
                    db,
                    identifier=owner.email if owner else "default",
                )
        if not result.get("success"):
            return {
                "success": False,
                "error": result.get("error") or "改官方角色失败",
                "error_code": result.get("error_code") or "role_update_failed",
                "status_code": result.get("status_code"),
            }
        return {
            "success": True,
            "email": target,
            "role": requested_role,
            "existing_role": current_role,
            "user_id": candidate,
            "message": f"已把 {target} 改成官方 {requested_role}",
            "data": result.get("data"),
        }

    async def invite_member(
        self,
        db: AsyncSession,
        workspace_id: int,
        email: str,
        role: str = "owner",
    ) -> dict[str, Any]:
        workspace = await self.load_workspace(db, workspace_id)
        if workspace is None:
            return {"success": False, "error": f"未找到 Workspace {workspace_id}", "error_code": "workspace_not_found"}
        try:
            requested_role = parse_invite_role(role)
        except ValueError:
            return {"success": False, "error": "邀请角色只能是 Owner 或 Member", "error_code": "invalid_invite_role"}
        owner = await self.owner_account(db, workspace)
        account_id = self._workspace_account_id(workspace)
        access_token = await self.ensure_access_token(db, workspace)
        target = normalize_email(email)
        if not access_token or not account_id:
            return {"success": False, "error": "该 Workspace 的登录凭证已过期，且自动刷新失败", "error_code": "token_refresh_failed"}
        result = await self.client.send_invite(
            access_token,
            account_id,
            target,
            db,
            identifier=owner.email if owner else "default",
            role=requested_role,
        )
        if not result.get("success") and is_access_token_error(result):
            refreshed = await self.ensure_access_token(db, workspace, force_refresh=True)
            if refreshed:
                result = await self.client.send_invite(
                    refreshed,
                    account_id,
                    target,
                    db,
                    identifier=owner.email if owner else "default",
                    role=requested_role,
                )
        if not result.get("success"):
            return {
                "success": False,
                "error": result.get("error") or "邀请失败",
                "error_code": result.get("error_code") or "invite_failed",
                "status_code": result.get("status_code"),
                "requested_role": requested_role,
            }
        return {
            "success": True,
            "message": f"已邀请 {target}",
            "data": result.get("data"),
            "requested_role": requested_role,
        }

    async def revoke_invite(self, db: AsyncSession, workspace_id: int, email: str) -> dict[str, Any]:
        workspace = await self.load_workspace(db, workspace_id)
        if workspace is None:
            return {"success": False, "error": f"未找到 Workspace {workspace_id}", "error_code": "workspace_not_found"}
        owner = await self.owner_account(db, workspace)
        account_id = self._workspace_account_id(workspace)
        access_token = await self.ensure_access_token(db, workspace)
        if not access_token or not account_id:
            return {"success": False, "error": "workspace token missing", "error_code": "token_refresh_failed"}
        result = await self.client.delete_invite(
            access_token,
            account_id,
            normalize_email(email),
            db,
            identifier=owner.email if owner else "default",
        )
        if not result.get("success") and not chatgpt_client.is_already_removed_error(
            result.get("status_code"), result.get("error"), result.get("error_code")
        ):
            return {"success": False, "error": result.get("error") or "撤回邀请失败", "error_code": result.get("error_code") or "revoke_failed"}
        # Do not mark local departure until the caller verifies the invitation is gone.
        return {"success": True, "message": f"{normalize_email(email)} 已撤回邀请"}


workspace_service = WorkspaceService()
