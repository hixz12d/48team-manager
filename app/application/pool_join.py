"""号池拉入任务编排：邀请 → 登录已有账号入组 → 授权 → 推送 Sub2API 并计数 → 改团队标签。"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.mailbox import mailbox_readiness_snapshot
from app.application.operations import operation_store
from app.core.time import utcnow
from app.domain.identity import MEMBERSHIP_STATE_JOINED
from app.integrations.openai.member_adapter import parse_invite_role, parse_invite_seat_intent
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership
from app.persistence.models.pool import StandbyPoolEntry

logger = logging.getLogger(__name__)

JOINABLE_STATES = {"pending", "failed"}
CONTINUABLE_STATES = {"failed", "manual_required"}
# Stops after the invitation went out: keep the invite and let the operator press "继续".
MANUAL_STATUSES = {"partial", "invited", "browser_failed", "not_joined"}
MANUAL_CODES = {
    "phone_verification_required", "cloudflare_challenge", "not_joined", "invite_link_missing",
    "invite_unverified", "membership_mismatch", "membership_changed", "oauth_failed",
}
# Never worth a retry on the same team without a human decision.
FAILED_CODES = {"account_not_registered"}
PUSH_FAILED_NOTE = "推送 Sub2API 失败，请到账号页重推"


def _fail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "error": message, **extra}


async def _joined_elsewhere(db: AsyncSession, account_id: int) -> bool:
    row = await db.scalar(select(WorkspaceMembership.id).where(
        WorkspaceMembership.account_id == int(account_id),
        WorkspaceMembership.membership_state == MEMBERSHIP_STATE_JOINED,
    ).limit(1))
    return row is not None


async def start_pool_join(
    db: AsyncSession,
    entry_id: int,
    *,
    workspace_id: int,
    role: str = "member",
    seat_intent: str = "workspace_default",
) -> dict[str, Any]:
    """为号池条目创建 pool_join 任务，拉入指定团队。"""
    entry = await db.get(StandbyPoolEntry, int(entry_id))
    if entry is None:
        return _fail("not_found", "号池条目不存在")
    if entry.state not in JOINABLE_STATES:
        return _fail("not_joinable", "当前状态不能拉入")
    return await _launch(db, entry, workspace_id=workspace_id, role=role, seat_intent=seat_intent)


async def continue_pool_join(db: AsyncSession, entry_id: int) -> dict[str, Any]:
    """接着上次的团队继续未完成的拉入（已邀请不重发、已入组直接授权）。"""
    entry = await db.get(StandbyPoolEntry, int(entry_id))
    if entry is None:
        return _fail("not_found", "号池条目不存在")
    if entry.state not in CONTINUABLE_STATES or not entry.workspace_id:
        return _fail("not_joinable", "当前状态不能继续")
    return await _launch(
        db, entry,
        workspace_id=int(entry.workspace_id),
        role=entry.role or "member",
        seat_intent=entry.seat_intent or "workspace_default",
    )


async def _launch(
    db: AsyncSession,
    entry: StandbyPoolEntry,
    *,
    workspace_id: int,
    role: str,
    seat_intent: str,
) -> dict[str, Any]:
    try:
        role = parse_invite_role(role, default="member")
        seat_intent = parse_invite_seat_intent(seat_intent).value
    except ValueError:
        return _fail("invalid_input", "角色或席位参数无效")

    account = await db.get(Account, int(entry.account_id))
    if account is None:
        return _fail("not_found", "号池条目对应的本地账号不存在")
    if not mailbox_readiness_snapshot(account)["ready"]:
        return _fail("not_joinable", "邮箱不可读，请先重新检测")
    if await _joined_elsewhere(db, account.id):
        return _fail("not_joinable", "该账号已在团队中")

    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None or workspace.status != "active":
        return _fail("not_found", "团队不存在或已停用")
    owner = await db.get(Account, int(workspace.owner_account_id)) if workspace.owner_account_id else None
    if owner is None or not owner.proxy:
        return _fail("not_joinable", "母号未配置代理")
    if workspace.seat_limit is not None and workspace.occupied_seats is not None \
            and workspace.occupied_seats >= workspace.seat_limit:
        return _fail("team_full", "团队席位已满")

    from app.application.manual_rotation import unresolved_for_workspace

    unresolved = await unresolved_for_workspace(db, workspace.id)
    if unresolved is not None:
        return _fail("rotation_unresolved", "该团队有未完成的手动轮转，请先处理", operation_id=unresolved.public_id)
    busy = await operation_store.browser_busy(db)
    if busy is not None:
        return _fail("browser_busy", "浏览器正被其他任务占用，请稍后再试", operation_id=busy.public_id)

    operation, blocker = await operation_store.create_workspace_locked(
        db,
        op_type="pool_join",
        workspace_id=workspace.id,
        account_id=account.id,
        email=account.email,
        input_payload={
            "entry_id": entry.id,
            "workspace_id": workspace.id,
            "role": role,
            "seat_intent": seat_intent,
        },
    )
    if blocker is not None:
        blocker_id = blocker.public_id
        await db.rollback()
        return _fail("operation_conflict", "该团队已有进行中的任务", operation_id=blocker_id)

    entry.state = "joining"
    entry.workspace_id = workspace.id
    entry.role = role
    entry.seat_intent = seat_intent
    entry.operation_public_id = operation.public_id
    entry.error_code = None
    entry.error = None
    await db.commit()

    target_entry, target_account, target_workspace = entry.id, account.id, workspace.id
    email, job_id = account.email, operation.public_id

    async def command(session: AsyncSession) -> dict[str, Any]:
        return await _run_pool_join(
            session,
            entry_id=target_entry,
            account_id=target_account,
            workspace_id=target_workspace,
            email=email,
            role=role,
            seat_intent=seat_intent,
            job_id=job_id,
        )

    from app.application.console_actions import _run_command

    launched = await _run_command(db, operation, command, background=True)
    return {**launched, "entry_id": target_entry, "workspace_id": target_workspace}


async def _set_entry(session: AsyncSession, entry_id: int, **values: Any) -> None:
    entry = await session.get(StandbyPoolEntry, int(entry_id))
    if entry is None:
        return
    for key, value in values.items():
        setattr(entry, key, value)
    await session.commit()


async def _run_pool_join(
    session: AsyncSession,
    *,
    entry_id: int,
    account_id: int,
    workspace_id: int,
    email: str,
    role: str,
    seat_intent: str,
    job_id: str,
) -> dict[str, Any]:
    from app.application.onboard import onboard_service

    try:
        from app.application.resources import hme as hme_service

        account = await session.get(Account, int(account_id))
        cfg = await hme_service.load_config(session)
        if account is None or not cfg.configured or not account.hme_account_id:
            error = "HME 未配置或该邮箱未绑定 HME 账号，无法读取邀请邮件和验证码"
            await _set_entry(session, entry_id, state="failed", error_code="mail_missing", error=error)
            return {"success": False, "status": "failed", "error_code": "mail_missing", "error": error,
                    "entry_id": entry_id}
        mailbox = {
            "hme_base_url": cfg.base_url,
            "hme_service_token": cfg.service_token,
            "hme_account_id": str(account.hme_account_id),
        }
        result = await onboard_service.invite_and_onboard(
            session,
            workspace_id=workspace_id,
            email_line=email,
            role=role,
            seat_intent=seat_intent,
            job_id=job_id,
            oauth_signup=True,
            use_phone_pool=True,
            login_existing=True,
            mailbox=mailbox,
            keep_operation_identity=True,
        )

        if result.get("success") and result.get("authorized"):
            return await _finish_joined(session, result, entry_id=entry_id, account_id=account_id,
                                        workspace_id=workspace_id, job_id=job_id)

        code = str(result.get("error_code") or "pool_join_failed")
        error = str(result.get("error") or "拉入未完成")
        status = str(result.get("status") or "")
        manual = (
            bool(result.get("partial") or result.get("joined") or result.get("success"))
            or status in MANUAL_STATUSES
            or code in MANUAL_CODES
        ) and code not in FAILED_CODES
        await _set_entry(session, entry_id, state="manual_required" if manual else "failed",
                         error_code=code[:60], error=error)
        return {
            **result,
            "success": False,
            "partial": False,
            "status": "manual_required" if manual else "failed",
            "error_code": code,
            "error": error,
            "entry_id": entry_id,
        }
    except Exception:
        logger.exception("pool join failed operation_id=%s", job_id)
        try:
            await session.rollback()
            await _set_entry(session, entry_id, state="failed", error_code="command_failed",
                             error="后台任务异常中断，请核对团队状态后点继续")
        except Exception:  # noqa: BLE001
            logger.exception("could not record pool entry failure entry_id=%s", entry_id)
        raise


async def _finish_joined(
    session: AsyncSession,
    result: dict[str, Any],
    *,
    entry_id: int,
    account_id: int,
    workspace_id: int,
    job_id: str,
) -> dict[str, Any]:
    from app.application import standby_pool
    from app.application.member_handoff import finish_after_authorization
    from app.application.onboard import onboard_service

    await onboard_service._progress(session, job_id=job_id, stage="pool_finish", message="正在推送 Sub2API 并计数")
    await session.commit()
    followups = await finish_after_authorization(
        session, account_id, workspace_id=workspace_id, push_sub2api=True, count_switch=True,
    )
    await session.commit()

    await onboard_service._progress(session, job_id=job_id, stage="pool_label", message="正在更新 HME 标签")
    await session.commit()
    try:
        await standby_pool.apply_team_label(session, entry_id, workspace_id)
        await session.commit()
    except Exception:  # noqa: BLE001 - the label never fails the join
        logger.exception("pool label failed entry_id=%s", entry_id)
        await session.rollback()

    await _set_entry(session, entry_id, state="joined", joined_at=utcnow(), error_code=None, error=None)

    email = str((result.get("child") or {}).get("email") or "")
    message = f"{email} 已入组并完成授权".strip()
    push = followups.get("sub2api") or {}
    if push.get("ok") is False:
        message = f"{message}；{PUSH_FAILED_NOTE}"
    return {
        **result,
        "success": True,
        "partial": False,
        "status": "active",
        "joined": True,
        "authorized": True,
        "message": message,
        "followups": followups,
        "entry_id": entry_id,
    }
