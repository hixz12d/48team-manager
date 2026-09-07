"""Auto rotate saga. Identity gate first; official 7d confirms weekly limit; vacancy gate stays on."""

from __future__ import annotations

import logging
from datetime import datetime, time, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.application.console_maintenance import purge_local_child_record
from app.application.identity import automation_gate
from app.application.operations import operation_store
from app.application.quota import quota_service
from app.application.settings import as_bool, get_setting_value
from app.application.workspaces import workspace_service
from app.core.config import load_settings
from app.core.time import utcnow
from app.domain.automation import WORKSPACE_LOCK_ACTIONS
from app.domain.identity import MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_UNKNOWN, PROVIDER_SUB2API
from app.domain.identity.ids import normalize_email
from app.domain.identity.policy import is_workspace_owner
from app.domain.rotate import (
    DEFAULT_AUTO_ROTATE_DAILY_LIMIT,
    DEFAULT_AUTO_ROTATE_DRAIN_SECONDS,
    DEFAULT_AUTO_ROTATE_ENABLED,
    DEFAULT_AUTO_ROTATE_FORCE_REFILL,
    DEFAULT_AUTO_ROTATE_ON_DEACTIVATED,
    DEFAULT_AUTO_ROTATE_ON_WEEKLY_LIMIT,
    classify_rotate_reason,
    daily_auto_rotate_limit_reached,
    official_weekly_limit_full,
    official_weekly_reset_at,
    rotate_backoff_at,
    rotate_terminal_status,
    should_unbind_sub2api,
)
from app.domain.vacancy import chatgpt_member_ids, is_safe_to_refill, summarize_for_message
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from app.persistence.models.operations import Operation

logger = logging.getLogger(__name__)


class RotateService:
    def __init__(self, *, sub2api=None, workspaces=None, quota=None, onboard=None):
        self.sub2api = sub2api or sub2api_client
        self.workspaces = workspaces or workspace_service
        self.quota = quota or quota_service
        if onboard is None:
            from app.application.onboard import onboard_service

            onboard = onboard_service
        self.onboard = onboard

    async def load_settings(self, db: AsyncSession) -> dict[str, Any]:
        env = load_settings()
        enabled_raw = await get_setting_value(db, "auto_rotate_enabled", str(bool(env.auto_rotate_enabled)).lower())
        deactivated_raw = await get_setting_value(db, "auto_rotate_on_deactivated", str(DEFAULT_AUTO_ROTATE_ON_DEACTIVATED).lower())
        weekly_raw = await get_setting_value(db, "auto_rotate_on_weekly_limit", str(DEFAULT_AUTO_ROTATE_ON_WEEKLY_LIMIT).lower())
        force_raw = await get_setting_value(db, "auto_rotate_force_refill", str(bool(env.force_refill)).lower())
        limit_raw = await get_setting_value(db, "auto_rotate_daily_limit", str(DEFAULT_AUTO_ROTATE_DAILY_LIMIT))
        try:
            daily_limit = max(0, int(limit_raw or DEFAULT_AUTO_ROTATE_DAILY_LIMIT))
        except (TypeError, ValueError):
            daily_limit = DEFAULT_AUTO_ROTATE_DAILY_LIMIT
        return {
            "auto_rotate_enabled": as_bool(enabled_raw, DEFAULT_AUTO_ROTATE_ENABLED) and bool(env.auto_rotate_enabled),
            "auto_rotate_on_deactivated": as_bool(deactivated_raw, DEFAULT_AUTO_ROTATE_ON_DEACTIVATED),
            "auto_rotate_on_weekly_limit": as_bool(weekly_raw, DEFAULT_AUTO_ROTATE_ON_WEEKLY_LIMIT),
            "auto_rotate_force_refill": as_bool(force_raw, DEFAULT_AUTO_ROTATE_FORCE_REFILL) and bool(env.force_refill),
            "auto_rotate_daily_limit": daily_limit,
        }

    async def _mark_step(
        self,
        db: AsyncSession,
        job_id: str | None,
        step_name: str,
        *,
        state: str,
        result: dict[str, Any] | None = None,
        error_code: str = "",
        error_message: str = "",
    ) -> None:
        if not job_id:
            return
        op = await operation_store.get_by_public_id(db, job_id)
        if op is None:
            return
        await operation_store.mark_step(
            db,
            op,
            step_name,
            state=state,
            result=result,
            error_code=error_code,
            error_message=error_message,
        )

    async def _active_child(
        self,
        db: AsyncSession,
        *,
        remote_id: Any = None,
        email: str = "",
    ) -> Account | None:
        target = normalize_email(email)
        remote = str(remote_id or "").strip()
        account = None
        if remote:
            binding = (
                await db.execute(
                    select(ExternalBinding).where(
                        ExternalBinding.provider == PROVIDER_SUB2API,
                        ExternalBinding.remote_account_id == remote,
                    )
                )
            ).scalar_one_or_none()
            if binding is not None:
                account = await db.get(Account, binding.local_account_id)
        if account is None and target:
            account = (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none()
        if account is None:
            return None
        if str(account.operational_state or "") != "active":
            return None
        if str(account.local_purpose or "") not in {"child", "standby"}:
            if str(account.local_purpose or "") != "child":
                return None
        return account

    async def _joined_workspace(self, db: AsyncSession, account: Account) -> Workspace | None:
        membership = (
            await db.execute(
                select(WorkspaceMembership)
                .options(selectinload(WorkspaceMembership.workspace))
                .where(
                    WorkspaceMembership.account_id == account.id,
                    WorkspaceMembership.membership_state.in_((MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_UNKNOWN)),
                )
                .order_by(WorkspaceMembership.id.desc())
            )
        ).scalars().first()
        if membership is None:
            return None
        return membership.workspace

    async def _remote_id_for(self, db: AsyncSession, account: Account) -> str:
        binding = (
            await db.execute(
                select(ExternalBinding).where(
                    ExternalBinding.provider == PROVIDER_SUB2API,
                    ExternalBinding.local_account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        return str(binding.remote_account_id or "") if binding is not None else ""

    async def count_today_auto_rotates(self, db: AsyncSession, workspace_id: int, now: datetime | None = None) -> int:
        stamp = now or utcnow()
        start = datetime.combine(stamp.date(), time.min, tzinfo=stamp.tzinfo)
        result = await db.execute(
            select(func.count())
            .select_from(Operation)
            .where(
                Operation.workspace_id == int(workspace_id),
                Operation.op_type == "rotate",
                Operation.source == "auto",
                Operation.state == "success",
                Operation.created_at >= start,
            )
        )
        return int(result.scalar() or 0)

    async def _confirm_weekly_limit(
        self,
        db: AsyncSession,
        *,
        account: Account | None,
        remote_id: Any,
        usage: Any = None,
        skip_fetch: bool = False,
    ) -> dict[str, Any]:
        if usage is not None:
            still_full = official_weekly_limit_full(usage)
            if still_full is True:
                return {"ok": True, "usage": usage, "next_eligible_at": official_weekly_reset_at(usage)}
            if still_full is False:
                return {"ok": False, "skipped": True, "code": "weekly_limit_not_confirmed", "usage": usage}
        if account is not None and not skip_fetch:
            try:
                snapshot = await self.quota.latest_official(db, account.id)
                if snapshot is None or not snapshot.success:
                    await self.quota.probe_account(db, account)
                    snapshot = await self.quota.latest_official(db, account.id)
            except Exception as exc:  # noqa: BLE001
                return {"ok": False, "failed": True, "code": "usage_confirm_failed", "error": str(exc)}
            if snapshot is None or not snapshot.success or snapshot.seven_day_used_percent is None:
                return {"ok": False, "skipped": True, "code": "weekly_limit_not_confirmed", "usage": snapshot}
            if int(snapshot.seven_day_used_percent) < 100:
                return {"ok": False, "skipped": True, "code": "weekly_limit_not_confirmed", "usage": snapshot}
            return {"ok": True, "usage": snapshot, "next_eligible_at": snapshot.seven_day_reset_at}
        remote = 0
        try:
            remote = int(remote_id or 0)
        except (TypeError, ValueError):
            remote = 0
        if not remote:
            return {"ok": False, "skipped": True, "code": "weekly_limit_missing_id", "error": "周限满缺少 Sub 账号 ID"}
        if skip_fetch:
            return {"ok": False, "skipped": True, "code": "weekly_limit_not_confirmed"}
        try:
            fetched = await self.sub2api.fetch_account_usage(db, remote, source="active", force=True)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "failed": True, "code": "usage_confirm_failed", "error": str(exc), "account_id": remote}
        still_full = official_weekly_limit_full(fetched)
        if still_full is not True:
            return {"ok": False, "skipped": True, "code": "weekly_limit_not_confirmed", "usage": fetched, "account_id": remote}
        return {"ok": True, "usage": fetched, "next_eligible_at": official_weekly_reset_at(fetched), "account_id": remote}

    async def _pause_and_drain(
        self,
        db: AsyncSession,
        *,
        job_id: str | None,
        remote_id: int,
        drain_seconds: int = DEFAULT_AUTO_ROTATE_DRAIN_SECONDS,
        in_test: bool = False,
    ) -> dict[str, Any]:
        if job_id:
            op = await operation_store.get_by_public_id(db, job_id)
            if op is not None and await operation_store.step_succeeded(db, op, "paused"):
                return {"ok": True, "skipped": True}
        if not remote_id:
            await self._mark_step(db, job_id, "paused", state="success", result={"skipped": True, "reason": "no_sub2api_id"})
            await self._mark_step(db, job_id, "drained", state="success", result={"skipped": True})
            return {"ok": True, "skipped": True}
        if in_test:
            paused = {"patched": False, "skipped": True}
        else:
            try:
                paused = await self.sub2api.set_account_schedulable(db, remote_id, False)
            except Exception as exc:  # noqa: BLE001
                await self._mark_step(db, job_id, "paused", state="failed", error_code="pause_failed", error_message=str(exc))
                return {"ok": False, "code": "pause_failed", "error": str(exc)}
        await self._mark_step(
            db,
            job_id,
            "paused",
            state="success",
            result={"account_id": remote_id, "patched": bool(paused.get("patched"))},
        )
        if job_id:
            op = await operation_store.get_by_public_id(db, job_id)
            if op is not None:
                await operation_store.note(db, op, "paused", f"已暂停 Sub2API 调度 account_id={remote_id}")
        wait_for = 0 if in_test else max(0, int(drain_seconds))
        if wait_for:
            import asyncio

            await asyncio.sleep(wait_for)
        await self._mark_step(db, job_id, "drained", state="success", result={"seconds": wait_for})
        return {"ok": True, "account_id": remote_id}

    async def _kick_joined_and_verify(
        self,
        db: AsyncSession,
        *,
        workspace: Workspace,
        email: str,
        live_item: dict[str, Any] | None,
        user_id: str | None,
    ) -> dict[str, Any]:
        ids = chatgpt_member_ids(user_id, live_item or {})
        if not ids:
            return {"success": False, "error": f"{email} 还在 Team 里，但没有可踢的 user_id", "error_code": "kick_missing_user_id"}
        last: dict[str, Any] = {"success": False, "error": "未执行踢人"}
        for candidate in ids:
            last = await self.workspaces.delete_member(db, workspace.id, candidate, email=email)
            if not last.get("success"):
                logger.warning("踢人 ID %s 失败: %s", candidate, last.get("error"))
                continue
            live, still = await self.workspaces.lookup_live_member(db, workspace, email)
            if live.get("success") is False:
                return {
                    "success": False,
                    "error": "官方踢人已经发出，但成员列表还对不上，没法确认这个邮箱是否踢掉。本地没有改成待命。请先同步，还在的话再踢一次。",
                    "error_code": "kick_unverified",
                }
            if still is None and live.get("success"):
                last["verified"] = True
                last["kicked_user_id"] = candidate
                return last
            if still and still.get("status") == "invited":
                return {"success": False, "status": "partial", "error_code": "invite_still_present",
                        "error": "成员已移出，但仍有待接受邀请；请核实后撤回邀请"}
        return {
            "success": False,
            "error": f"{email} 没有踢掉，ChatGPT 里还在。不要信刚才的成功提示。",
            "error_code": "kick_not_removed",
        }

    async def kick_to_standby(
        self,
        db: AsyncSession,
        *,
        workspace_id: int,
        email: str,
        user_id: str | None = None,
        reason: str = "",
        next_eligible_at: datetime | None = None,
        unbind_sub2api: bool = False,
        purge_local: bool = False,
        invitation_only: bool = False,
        job_id: str | None = None,
    ) -> dict[str, Any]:
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
        workspace = await self.workspaces.load_workspace(db, workspace_id)
        if workspace is None:
            return {"success": False, "error": f"未找到 Workspace {workspace_id}", "error_code": "workspace_not_found"}
        target = normalize_email(email)
        child = (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none()
        if job_id:
            op = await operation_store.get_by_public_id(db, job_id)
            if op is not None:
                cancelled = await operation_store.check_cancel(db, op, destructive_started=False)
                if cancelled:
                    return cancelled
        live, live_item = await self.workspaces.lookup_live_member(db, workspace, target)
        lookup_state = live.get("lookup_state") or ("found" if live_item else ("absent_confirmed" if live.get("success") else "unknown_due_to_error"))
        if lookup_state == "unknown_due_to_error" or live.get("success") is False and live_item is None and lookup_state != "absent_confirmed":
            return {
                "success": False,
                "status": "manual_required",
                "error": f"{live.get('error') or '读取成员失败'}，上游状态未知，未执行踢人/撤回，也未改本地状态",
                "error_code": live.get("error_code") or "kick_lookup_unknown",
            }
        owner = None
        owner_fn = getattr(self.workspaces, "owner_account", None)
        if callable(owner_fn):
            owner = await owner_fn(db, workspace)
        elif workspace.owner_account_id:
            owner = await db.get(Account, workspace.owner_account_id)
        if child is not None and is_workspace_owner(workspace, child.id):
            return {"success": False, "error": "不能踢出当前工作区的主控母号", "error_code": "primary_mother_protected"}
        if owner is not None and normalize_email(owner.email) == target:
            return {"success": False, "error": "不能踢出当前工作区的主控母号", "error_code": "primary_mother_protected"}
        guard_fn = getattr(self.workspaces, "_last_owner_guard", None)
        if callable(guard_fn):
            guard = guard_fn(workspace, owner, live, live_item, email=target)
            if guard is not None:
                return guard
        live_status = (live_item or {}).get("status")
        if live_item:
            live_id = live_item.get("user_id") or self.workspaces.client.pick_user_id(live_item)
            if live_id:
                user_id = live_id
        should_revoke = live_status == "invited"
        if invitation_only and live_status == "joined":
            return {"success": False, "error_code": "already_joined", "error": "该账号已经加入团队，未执行撤回或踢人"}
        if should_revoke:
            result = await self.workspaces.revoke_invite(db, workspace.id, target)
            if not result.get("success"):
                return {"success": False, "error": result.get("error") or "撤回邀请失败", "error_code": "revoke_failed"}
            after, remaining = await self.workspaces.lookup_live_member(db, workspace, target)
            if not after.get("success") or remaining is not None:
                return {"success": False, "status": "partial", "error_code": "revoke_unverified",
                        "error": "撤回已提交，但尚未确认官方记录消失；保留本地状态，请同步后核实"}
            result = {"success": True, "status": "revoked", "message": f"{target} 已撤回邀请"}
        elif lookup_state == "absent_confirmed" and live_item is None:
            result = {"success": True, "message": f"{target} 官方成员和邀请都不存在", "already_absent": True}
        else:
            result = await self._kick_joined_and_verify(
                db,
                workspace=workspace,
                email=target,
                live_item=live_item,
                user_id=user_id,
            )
        if not result.get("success"):
            return {**result, "success": False, "error": result.get("error") or "踢人失败", "error_code": result.get("error_code") or "kick_failed"}
        from app.application.member_lifecycle import has_other_active_context, record_confirmed_departure
        await record_confirmed_departure(db, workspace, target)
        other_context = bool(child and await has_other_active_context(db, child, workspace.id))
        unbind = bool(unbind_sub2api or should_unbind_sub2api(reason) or purge_local)
        deleted_sub = None
        remote_unbind_confirmed = False
        binding_error = None
        remote_id = await self._remote_id_for(db, child) if child is not None else ""
        if child and unbind and remote_id:
            try:
                deleted_sub = await self.sub2api.delete_accounts(db, [int(remote_id)])
                remote_unbind_confirmed = True
            except Exception as exc:  # noqa: BLE001
                binding_error = str(exc)
                logger.warning("Sub2API 下架失败 email=%s error=%s", target, exc)
        purged = False
        if child and purge_local and not (unbind and remote_id and not remote_unbind_confirmed):
            purged_local = await purge_local_child_record(db, workspace, child)
            if not purged_local.get("ok"):
                return {
                    "success": False,
                    "error": purged_local.get("error") or "本地档案删除失败",
                    "error_code": purged_local.get("error_code") or "purge_failed",
                }
            purged = True
            child = None
        elif child and not other_context:
            await self.workspaces.mark_standby(
                db,
                child,
                next_eligible_at=next_eligible_at,
                unbind_sub2api=unbind,
                remote_unbind_confirmed=remote_unbind_confirmed,
                binding_error=binding_error,
            )
        await db.flush()
        vacancy = result.get("vacancy")
        if purged:
            message = f"{target} 已永久删除：官方席位已处理，本地档案已清除"
            status = "purged"
        elif child:
            message = f"{target} 已离开本团队，账号档案已保留" + ("，其他团队不受影响" if other_context else "，可重新邀请")
            status = "departed" if other_context else "standby"
        else:
            message = f"{target} 已踢出官方席位"
            status = "kicked"
        success = True
        if unbind and remote_id and not remote_unbind_confirmed:
            message = f"{message}，官方已踢出但 Sub2API 下架失败，本地 Binding 已保留"
            status = "partial"
            success = False
        elif unbind and remote_unbind_confirmed:
            message = f"{message}，已从 Sub 下架"
        summary = summarize_for_message(vacancy)
        if summary:
            message = f"{message}。{summary}"
        return {
            "success": success,
            "status": status,
            "partial": status == "partial",
            "message": message,
            "child": {"id": child.id, "email": child.email} if child else None,
            "vacancy": vacancy,
            "purged": purged,
            "unbound_sub2api": bool(unbind and remote_unbind_confirmed),
            "deleted_sub2api": deleted_sub,
            "error": binding_error if status == "partial" else None,
            "error_code": "sub2api_unbind_failed" if status == "partial" else None,
        }

    async def kick_and_refill(
        self,
        db: AsyncSession,
        *,
        workspace_id: int,
        email: str,
        email_line: str = "",
        phone_line: str = "",
        proxy: str = "",
        child_id: int | None = None,
        force_refill: bool = False,
        job_id: str | None = None,
        reason: str = "",
        next_eligible_at: datetime | None = None,
        refill: Any = None,
        in_test: bool = False,
        role: str = "owner",
    ) -> dict[str, Any]:
        target = normalize_email(email)
        if not target:
            return {"success": False, "error": "缺少要踢的子号邮箱", "error_code": "rotate_email_missing"}
        already_kicked = False
        if job_id:
            op = await operation_store.get_by_public_id(db, job_id)
            already_kicked = bool(op and await operation_store.step_succeeded(db, op, "kicked"))
        if already_kicked:
            child = (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none()
            kick_result = {
                "success": True,
                "status": "standby",
                "message": f"{target} 本任务已踢过，跳过重复踢人",
                "child": {"id": child.id, "email": child.email} if child else None,
                "skipped_duplicate_kick": True,
            }
        else:
            kick_result = await self.kick_to_standby(
                db,
                workspace_id=workspace_id,
                email=target,
                reason=reason,
                next_eligible_at=next_eligible_at,
                unbind_sub2api=should_unbind_sub2api(reason),
                job_id=job_id,
            )
            if not kick_result.get("success"):
                await self._mark_step(
                    db,
                    job_id,
                    "kicked",
                    state="failed",
                    result=kick_result,
                    error_code=str(kick_result.get("error_code") or "kick_failed"),
                    error_message=str(kick_result.get("error") or "踢人失败"),
                )
                return kick_result
            await self._mark_step(db, job_id, "kicked", state="success", result=kick_result)

        vacancy = kick_result.get("vacancy")
        if not force_refill and not kick_result.get("skipped_duplicate_kick") and not is_safe_to_refill(vacancy):
            summary = summarize_for_message(vacancy) or "踢人回执不能证明席位已释放"
            return {
                "success": False,
                "error": f"已踢出 {target}，但{summary}。已停止自动补位，核对 Billing 后可勾选强制补位。",
                "error_code": "vacancy_not_safe_to_refill",
                "needs_confirm": True,
                "kick": kick_result,
                "vacancy": vacancy,
                "rotated": True,
            }

        refill_fn = refill or self.onboard.refill
        invite_result = await refill_fn(
            db,
            workspace_id=workspace_id,
            email_line=email_line,
            phone_line=phone_line,
            proxy=proxy,
            child_id=child_id,
            force_refill=force_refill,
            job_id=job_id,
            skip_email=target,
            in_test=in_test,
            role=role,
        )
        if not invite_result.get("success"):
            return {
                "success": False,
                "error": invite_result.get("error") or "补位失败，未完成轮转",
                "kick": kick_result,
                "invite": invite_result,
            }
        vacancy = kick_result.get("vacancy")
        invited_email = ((invite_result.get("child") or {}) or {}).get("email") or email_line or ""
        message = f"已踢出 {target}" + (f" 并补入 {invited_email}" if invited_email else "")
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

    async def run_rotate_saga(
        self,
        db: AsyncSession,
        *,
        job_id: str,
        workspace_id: int,
        email: str,
        reason: str,
        force_refill: bool = False,
        next_eligible_at: datetime | None = None,
        email_line: str = "",
        phone_line: str = "",
        proxy: str = "",
        child_id: int | None = None,
        account: dict[str, Any] | None = None,
        usage: Any = None,
        skip_confirm: bool = False,
        now: datetime | None = None,
        refill: Any = None,
        in_test: bool = False,
        role: str = "owner",
    ) -> dict[str, Any]:
        del now
        target = normalize_email(email)
        payload_account = dict(account or {})
        child = (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none() if target else None
        remote_id = payload_account.get("id") or (await self._remote_id_for(db, child) if child is not None else None)
        op = await operation_store.get_by_public_id(db, job_id) if job_id else None
        already_kicked = bool(op and await operation_store.step_succeeded(db, op, "kicked"))
        if not already_kicked:
            busy = await operation_store.active_for_workspace(
                db,
                workspace_id,
                actions=WORKSPACE_LOCK_ACTIONS,
                exclude_public_id=job_id,
            )
            if busy is not None:
                return {
                    "success": False,
                    "error": f"Workspace {workspace_id} 已有 {busy.op_type} 任务 {busy.public_id} 在跑",
                    "error_code": "operation_conflict",
                    "operation_id": busy.public_id,
                }
            gate = await automation_gate(db, remote_account_id=remote_id, email=target, workspace_id=workspace_id)
            if not gate.get("allow"):
                await self._mark_step(
                    db,
                    job_id,
                    "confirm_trigger",
                    state="failed",
                    result=gate,
                    error_code=str(gate.get("error_code") or "identity_conflict"),
                    error_message=str(gate.get("reason") or "身份门闩拒绝自动踢拉"),
                )
                return {
                    "success": False,
                    "error": gate.get("reason") or "身份门闩拒绝自动踢拉",
                    "error_code": gate.get("error_code") or "identity_conflict",
                    "status": "manual_required",
                }

        confirmed = {"ok": True, "usage": usage, "next_eligible_at": next_eligible_at, "code": ""}
        if not already_kicked:
            if op is not None and await operation_store.step_succeeded(db, op, "confirm_trigger"):
                confirmed = {"ok": True, "usage": usage, "next_eligible_at": next_eligible_at, "code": ""}
            elif skip_confirm:
                confirmed = {"ok": True, "usage": usage, "next_eligible_at": next_eligible_at, "code": ""}
                await self._mark_step(db, job_id, "confirm_trigger", state="success", result={"reason": reason, "skipped": True})
            elif reason == "weekly_limit":
                confirmed = await self._confirm_weekly_limit(
                    db,
                    account=child,
                    remote_id=remote_id,
                    usage=usage,
                    skip_fetch=False,
                )
                if confirmed.get("ok"):
                    await self._mark_step(
                        db,
                        job_id,
                        "confirm_trigger",
                        state="success",
                        result={"reason": reason},
                    )
                else:
                    await self._mark_step(
                        db,
                        job_id,
                        "confirm_trigger",
                        state="failed" if confirmed.get("failed") else "success",
                        result=confirmed,
                        error_code=str(confirmed.get("code") or ""),
                        error_message=str(confirmed.get("error") or confirmed.get("code") or ""),
                    )
                    return {
                        "success": False,
                        "error": confirmed.get("error") or "周限未确认，未踢人",
                        "error_code": confirmed.get("code") or "weekly_limit_not_confirmed",
                        "skipped": bool(confirmed.get("skipped")),
                        "failed": bool(confirmed.get("failed")),
                        "usage": confirmed.get("usage"),
                    }
            else:
                await self._mark_step(db, job_id, "confirm_trigger", state="success", result={"reason": reason})
            eligible = confirmed.get("next_eligible_at") or next_eligible_at
            try:
                remote_int = int(remote_id or 0)
            except (TypeError, ValueError):
                remote_int = 0
            pause = await self._pause_and_drain(db, job_id=job_id, remote_id=remote_int, in_test=in_test)
            if not pause.get("ok"):
                return {
                    "success": False,
                    "error": pause.get("error") or "暂停 Sub2API 调度失败，未踢人",
                    "error_code": pause.get("code") or "pause_failed",
                }
        else:
            eligible = next_eligible_at

        result = await self.kick_and_refill(
            db,
            workspace_id=workspace_id,
            email=target,
            email_line=email_line,
            phone_line=phone_line,
            proxy=proxy,
            child_id=child_id,
            force_refill=force_refill,
            role=role,
            job_id=job_id,
            reason=reason,
            next_eligible_at=eligible,
            refill=refill,
            in_test=in_test,
        )
        result.setdefault("reason", reason)
        if result.get("error_code") == "vacancy_not_safe_to_refill":
            await self._mark_step(
                db,
                job_id,
                "refill",
                state="manual_required",
                result=result,
                error_code="vacancy_not_safe_to_refill",
                error_message=str(result.get("error") or "vacancy 闸拦住补位"),
            )
        elif result.get("success"):
            await self._mark_step(db, job_id, "refill", state="success", result=result)
        return result

    async def run_once(
        self,
        db: AsyncSession,
        *,
        now: datetime | None = None,
        settings: dict[str, Any] | None = None,
        accounts: list[dict[str, Any]] | None = None,
        refill: Any = None,
        in_test: bool = False,
    ) -> dict[str, Any]:
        cfg = settings or await self.load_settings(db)
        stamp = now or utcnow()
        daily_limit = int(cfg.get("auto_rotate_daily_limit") or DEFAULT_AUTO_ROTATE_DAILY_LIMIT)
        stats: dict[str, Any] = {
            "enabled": bool(cfg.get("auto_rotate_enabled")),
            "scanned": 0,
            "rotated": 0,
            "kicked_only": 0,
            "skipped": 0,
            "failed": 0,
            "capped": 0,
            "conflict": 0,
            "email": "",
            "reason": "",
        }
        if not cfg.get("auto_rotate_enabled"):
            stats["skipped"] = 1
            return stats
        busy = await operation_store.browser_busy(db)
        if busy:
            stats["skipped"] = 1
            stats["email"] = busy.email or ""
            return stats
        remote_accounts = accounts if accounts is not None else await self.sub2api.list_status_accounts(db)
        occupied: set[int] = set()
        for op_row in await operation_store.iter_running(db, WORKSPACE_LOCK_ACTIONS):
            if op_row.workspace_id:
                occupied.add(int(op_row.workspace_id))
        candidates: list[tuple[datetime, dict[str, Any], Account, Workspace, str]] = []
        for remote in remote_accounts:
            remote_id = remote.get("id")
            schedule = self.sub2api.schedule_kind(remote)
            email = self.sub2api.account_email(remote)
            child = await self._active_child(db, remote_id=remote_id, email=email)
            last_reauth = str(child.last_reauth_code or "") if child is not None else ""
            reason = classify_rotate_reason(
                kind=schedule.get("kind") or "",
                last_reauth_code=last_reauth,
                on_deactivated=bool(cfg.get("auto_rotate_on_deactivated", True)),
                on_weekly_limit=bool(cfg.get("auto_rotate_on_weekly_limit", True)),
            )
            if not reason:
                continue
            if child is None:
                stats["skipped"] += 1
                continue
            if child.next_eligible_at and child.next_eligible_at > stamp:
                continue
            workspace = await self._joined_workspace(db, child)
            if workspace is None or workspace.id in occupied:
                stats["skipped"] += 1
                continue
            gate = await automation_gate(db, remote_account_id=remote_id, email=email, workspace_id=workspace.id)
            if not gate.get("allow"):
                stats["skipped"] += 1
                if gate.get("error_code") == "identity_conflict":
                    stats["conflict"] += 1
                    if child is not None:
                        child.last_reauth_code = "identity_conflict"
                continue
            due_at = child.next_eligible_at or stamp
            candidates.append((due_at, remote, child, workspace, reason))
        stats["scanned"] = len(candidates)
        if not candidates:
            await db.commit()
            return stats
        candidates.sort(key=lambda item: (item[0], int(item[2].id)))
        remote, child, workspace, reason = candidates[0][1], candidates[0][2], candidates[0][3], candidates[0][4]
        email = self.sub2api.account_email(remote) or child.email
        today_count = await self.count_today_auto_rotates(db, workspace.id, stamp)
        if daily_auto_rotate_limit_reached(today_count, daily_limit):
            stats["capped"] = 1
            stats["skipped"] = 1
            stats["email"] = email
            return stats
        confirmed = {"ok": True, "usage": None, "next_eligible_at": None}
        if reason == "weekly_limit":
            confirmed = await self._confirm_weekly_limit(db, account=child, remote_id=remote.get("id"))
            if not confirmed.get("ok"):
                code = str(confirmed.get("code") or "")
                stats["email"] = email
                stats["reason"] = reason
                if code == "weekly_limit_not_confirmed":
                    child.next_eligible_at = stamp + timedelta(hours=1)
                    stats["skipped"] = 1
                elif code == "usage_confirm_failed":
                    child.next_eligible_at = rotate_backoff_at(stamp, int(child.quota_probe_fail_count or 0) + 1)
                    stats["failed"] = 1
                else:
                    stats["skipped"] = 1
                await db.commit()
                return stats
        next_eligible_at = confirmed.get("next_eligible_at")
        op = await operation_store.create(
            db,
            op_type="rotate",
            workspace_id=workspace.id,
            account_id=child.id,
            email=email,
            input_payload={
                "workspace_id": workspace.id,
                "email": email,
                "reason": reason,
                "force_refill": bool(cfg.get("auto_rotate_force_refill")),
                "account_id": remote.get("id"),
            },
            source="auto",
        )
        result = await self.run_rotate_saga(
            db,
            job_id=op.public_id,
            workspace_id=workspace.id,
            email=email,
            reason=reason,
            force_refill=bool(cfg.get("auto_rotate_force_refill")),
            next_eligible_at=next_eligible_at,
            account=remote,
            usage=confirmed.get("usage"),
            skip_confirm=True,
            now=stamp,
            refill=refill,
            in_test=in_test,
        )
        code = str(result.get("error_code") or "")
        status = rotate_terminal_status(success=bool(result.get("success")), error_code=code)
        await operation_store.finish(db, op, {"success": bool(result.get("success")), "status": status, **result})
        stats["email"] = email
        stats["reason"] = reason
        if result.get("success"):
            stats["rotated"] = 1
            child.next_eligible_at = stamp + timedelta(hours=12)
        elif code == "vacancy_not_safe_to_refill":
            stats["kicked_only"] = 1
            child.next_eligible_at = stamp + timedelta(hours=12)
        elif code == "identity_conflict":
            stats["conflict"] = 1
            stats["failed"] = 1
        else:
            stats["failed"] = 1
            child.next_eligible_at = rotate_backoff_at(stamp, 1)
        await db.commit()
        return stats


rotate_service = RotateService()
