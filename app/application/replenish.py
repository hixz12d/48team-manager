"""One-seat Team replenish. Claim HME, invite, authorize in the same operation."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.application.mailbox import probe_account_mailbox
from app.application.onboard import onboard_service, serialize_child
from app.application.operations import operation_store
from app.application.reauth import reauth_service
from app.application.resources import hme as hme_service
from app.domain.automation import WORKSPACE_LOCK_ACTIONS
from app.persistence.models.identity import Account, Workspace


class ReplenishService:
    def __init__(self, *, onboard=None, reauth=None):
        self.onboard = onboard or onboard_service
        self.reauth = reauth or reauth_service

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
                .options(selectinload(Workspace.owner_account))
                .where(Workspace.id == int(workspace_id))
            )
        ).scalar_one_or_none()

    async def _attach_child(self, db: AsyncSession, job_id: str | None, child: dict[str, Any] | None) -> None:
        if not job_id or not child:
            return
        op = await operation_store.get_by_public_id(db, job_id)
        if op is None:
            return
        email = str(child.get("email") or "").strip()
        account_id = child.get("id")
        if email:
            op.email = email
        if account_id:
            op.account_id = int(account_id)
        await db.flush()

    def _preflight(self, workspace: Workspace) -> dict[str, Any] | None:
        owner = workspace.owner_account
        if owner is None:
            return {
                "success": False,
                "error": "该团队没有绑定本地母号，无法自动补人",
                "error_code": "owner_account_missing",
                "status": "blocked",
            }
        if not str(owner.proxy or "").strip():
            return {
                "success": False,
                "error": "母号尚未配置静态 ISP 代理，自动补人跑不了",
                "error_code": "proxy_missing",
                "status": "blocked",
            }
        capacity = workspace.seat_limit
        occupied = getattr(workspace, "occupied_seats", None)
        if capacity is not None and occupied is not None and occupied >= capacity:
            return {
                "success": False,
                "error": f"占用 {occupied}/{capacity}，不能再补人",
                "error_code": "team_full",
                "status": "blocked",
            }
        return None

    async def run(
        self,
        db: AsyncSession,
        *,
        workspace_id: int,
        job_id: str | None = None,
        role: str = "owner",
        phone_line: str = "",
        in_test: bool = False,
    ) -> dict[str, Any]:
        workspace = await self._load_workspace(db, workspace_id)
        if workspace is None:
            return {"success": False, "error": f"未找到 Workspace {workspace_id}", "error_code": "workspace_not_found"}

        busy = await operation_store.active_for_workspace(
            db,
            workspace.id,
            actions=WORKSPACE_LOCK_ACTIONS,
            exclude_public_id=job_id,
        )
        if busy is not None:
            return {
                "success": False,
                "error": f"Workspace {workspace.id} 已有 {busy.op_type} 任务 {busy.public_id} 在跑，避免两边同时踢拉",
                "error_code": "operation_conflict",
                "operation_id": busy.public_id,
            }

        browser_busy = await operation_store.browser_busy(db)
        if browser_busy is not None and browser_busy.public_id != job_id:
            return {
                "success": False,
                "error": f"已有浏览器任务 {browser_busy.email or browser_busy.public_id}",
                "error_code": "browser_busy",
                "operation_id": browser_busy.public_id,
            }

        blocked = self._preflight(workspace)
        if blocked is not None:
            await self._progress(
                db,
                job_id=job_id,
                stage="blocked",
                message=str(blocked.get("error") or "无法补充"),
                error=str(blocked.get("error") or ""),
                error_code=str(blocked.get("error_code") or ""),
            )
            return blocked

        cfg = await hme_service.load_config(db)
        if not cfg.configured:
            await self._progress(db, job_id=job_id, stage="hme_failed", message="HME 未配置", error="HME 未配置", error_code="hme_unconfigured")
            return {"success": False, "error": "HME 未配置", "error_code": "hme_unconfigured", "status": "hme_failed"}

        await self._progress(db, job_id=job_id, stage="hme", message="正在领取未占用 HME 别名")
        onboard_result = await self.onboard.invite_and_onboard(
            db,
            workspace_id=workspace.id,
            email_line="",
            phone_line=phone_line,
            reuse_existing=True,
            job_id=job_id,
            in_test=in_test,
            role=role,
        )
        child = onboard_result.get("child") if isinstance(onboard_result, dict) else None
        await self._attach_child(db, job_id, child if isinstance(child, dict) else None)
        if not onboard_result.get("success"):
            return {
                "success": False,
                "error": onboard_result.get("error") or "拉人失败",
                "error_code": onboard_result.get("error_code") or "onboard_failed",
                "status": onboard_result.get("status") or "onboard_failed",
                "child": child,
                "onboard": onboard_result,
            }

        account_id = (child or {}).get("id") if isinstance(child, dict) else None
        if not account_id:
            return {
                "success": False,
                "error": "拉人成功但没有本地账号",
                "error_code": "identity_unbound",
                "status": "onboard_failed",
                "onboard": onboard_result,
            }

        account = await db.get(Account, int(account_id))
        if account is None:
            return {
                "success": False,
                "error": "拉人成功但本地账号不存在",
                "error_code": "identity_unbound",
                "status": "onboard_failed",
                "child": child,
                "onboard": onboard_result,
            }

        account.auto_reauth_opt_in = True
        await db.flush()
        await self._progress(db, job_id=job_id, stage="mailbox", message="正在绑定并检测邮箱")
        mailbox = await probe_account_mailbox(db, account.id)
        await db.refresh(account)

        await self._progress(db, job_id=job_id, stage="authorizing", message="正在自动授权，不依赖定时扫描")
        reauth_result = await self.reauth.run_immediate_reauth(
            db,
            account,
            progress_job_id=job_id,
            skip_browser_busy=True,
        )
        child_payload = serialize_child(account)
        if not reauth_result.get("success") or reauth_result.get("skipped"):
            error = reauth_result.get("error") or "入组成功，但自动授权未完成"
            code = str(reauth_result.get("error_code") or "reauth_failed")
            await self._progress(db, job_id=job_id, stage="auth_failed", message=error, error=error, error_code=code)
            return {
                "success": False,
                "ok": False,
                "partial": True,
                "status": "partial",
                "error": error,
                "error_code": code,
                "account_id": account.id,
                "email": account.email,
                "child": child_payload,
                "onboard": onboard_result,
                "mailbox": mailbox,
                "reauth": reauth_result,
                "message": f"{account.email} 已入组，但授权未完成。可对该账号重试自动授权，不要再领新号。",
            }

        message = f"{account.email} 已入组并完成授权。本轮未推送 Sub2API"
        await self._progress(db, job_id=job_id, stage="authorized", message=message)
        return {
            "success": True,
            "status": "active",
            "message": message,
            "account_id": account.id,
            "email": account.email,
            "child": child_payload,
            "onboard": onboard_result,
            "mailbox": mailbox,
            "reauth": reauth_result,
            "pushed": False,
        }


replenish_service = ReplenishService()
