"""号池拉入任务编排：（可选）移出被替换的子号 → 邀请 → 登录已有账号入组 → 授权 → 推送（Sub2API 或 codex-rs）并计数 → 改团队标签。"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.mailbox import mailbox_readiness_snapshot
from app.application.operations import operation_store, unpack_input
from app.core.time import utcnow
from app.domain.identity import MEMBERSHIP_STATE_JOINED
from app.domain.identity.ids import normalize_email
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
# Kick outcomes where the official side may already have changed: check, then press "继续".
KICK_MANUAL_STATUSES = {"partial", "awaiting_confirmation", "manual_required", "state_changed"}
# Never worth a retry on the same team without a human decision.
FAILED_CODES = {"account_not_registered"}
PUSH_FAILED_NOTE = "推送 Sub2API 失败，请到账号页重推"
CODEX_RS_FAILED_NOTE = "导入 codex-rs 失败，请到账号页重新导入"
PUSH_TARGETS = ("sub2api", "codex_rs")


def _fail(code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "error": message, **extra}


async def _push_target(db: AsyncSession, value: Any) -> str:
    """Explicit target wins; otherwise follow the "授权后推送到" switch."""
    if value in PUSH_TARGETS:
        return str(value)
    from app.application.settings import load_auth_push_target

    return await load_auth_push_target(db)


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
    replace_email: str = "",
    push_target: str | None = None,
    source: str = "manual",
) -> dict[str, Any]:
    """为号池条目创建 pool_join 任务，拉入指定团队；replace_email 非空时先移出该子号腾席位。

    push_target 为 None 时跟随"授权后推送到"开关；source 记到任务来源（自动补号传 codex_refill）。
    """
    entry = await db.get(StandbyPoolEntry, int(entry_id))
    if entry is None:
        return _fail("not_found", "号池条目不存在")
    if entry.state not in JOINABLE_STATES:
        return _fail("not_joinable", "当前状态不能拉入")
    return await _launch(db, entry, workspace_id=workspace_id, role=role, seat_intent=seat_intent,
                         replace_email=replace_email, push_target=await _push_target(db, push_target),
                         source=source or "manual")


async def continue_pool_join(db: AsyncSession, entry_id: int) -> dict[str, Any]:
    """接着上次的团队继续未完成的拉入（已移出不重踢、已邀请不重发、已入组直接授权）。

    推送去向和任务来源沿用上一次任务，自动补号的号人工点"继续"也会导入 codex-rs。
    """
    entry = await db.get(StandbyPoolEntry, int(entry_id))
    if entry is None:
        return _fail("not_found", "号池条目不存在")
    if entry.state not in CONTINUABLE_STATES or not entry.workspace_id:
        return _fail("not_joinable", "当前状态不能继续")
    previous = await operation_store.get_by_public_id(db, entry.operation_public_id or "")
    context = unpack_input(previous.input_json) if previous is not None else {}
    replace_email = str(context.get("replace_email") or "")
    replace_done = bool(context.get("replace_done")) or bool(
        replace_email and previous is not None
        and await operation_store.step_succeeded(db, previous, "official_removed")
    )
    source = str(getattr(previous, "source", None) or "manual")
    return await _launch(
        db, entry,
        workspace_id=int(entry.workspace_id),
        role=entry.role or "member",
        seat_intent=entry.seat_intent or "workspace_default",
        replace_email=replace_email,
        replace_done=replace_done,
        push_target=await _push_target(db, context.get("push_target")),
        source=source,
    )


async def _launch(
    db: AsyncSession,
    entry: StandbyPoolEntry,
    *,
    workspace_id: int,
    role: str,
    seat_intent: str,
    replace_email: str = "",
    replace_done: bool = False,
    push_target: str = "sub2api",
    source: str = "manual",
) -> dict[str, Any]:
    try:
        role = parse_invite_role(role, default="member")
        seat_intent = parse_invite_seat_intent(seat_intent).value
    except ValueError:
        return _fail("invalid_input", "角色或席位参数无效")
    replace_email = normalize_email(replace_email)

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
    if replace_email:
        if replace_email == normalize_email(owner.email):
            return _fail("not_joinable", "不能替换母号")
        if replace_email == normalize_email(account.email):
            return _fail("invalid_input", "被替换的子号不能是号池里这个号")
    elif workspace.seat_limit is not None and workspace.occupied_seats is not None \
            and workspace.occupied_seats >= workspace.seat_limit:
        return _fail("team_full", "团队席位已满，请选一个子号替换")

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
            "replace_email": replace_email,
            "replace_done": replace_done,
            "push_target": push_target,
        },
        source=source,
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
            replace_email=replace_email,
            replace_done=replace_done,
            push_target=push_target,
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
    replace_email: str = "",
    replace_done: bool = False,
    push_target: str = "sub2api",
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
        stopped = await _replace_member(session, entry_id=entry_id, workspace_id=workspace_id, job_id=job_id,
                                        replace_email=replace_email, replace_done=replace_done)
        if stopped is not None:
            return stopped
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
                                        workspace_id=workspace_id, job_id=job_id, push_target=push_target)

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


async def _replace_member(
    session: AsyncSession,
    *,
    entry_id: int,
    workspace_id: int,
    job_id: str,
    replace_email: str,
    replace_done: bool,
) -> dict[str, Any] | None:
    """先把被替换的子号移出官方团队（远端只暂停调度、不删），腾出席位。返回 None 表示可以继续邀请。"""
    from app.application.onboard import onboard_service
    from app.application.rotate import rotate_service
    from app.domain.vacancy import is_billable_vacancy

    op = await operation_store.get_by_public_id(session, job_id)
    if not replace_email or replace_done:
        if op is not None:
            result = {"already_removed": replace_email} if replace_email else {"skipped": True}
            await operation_store.mark_step(session, op, "official_removed", state="success", result=result)
            await session.commit()
        return None

    await onboard_service._progress(session, job_id=job_id, stage="kicking", message=f"正在移出 {replace_email}")
    await session.commit()
    kicked = await rotate_service.kick_to_standby(
        session, workspace_id=workspace_id, email=replace_email, reason="pool_replace", job_id=job_id,
    )
    await session.commit()
    removed = op is not None and await operation_store.step_succeeded(session, op, "official_removed")
    if removed and is_billable_vacancy(kicked.get("vacancy")):
        error = f"{replace_email} 已移出，但官方回执显示空出的席位要计费；确认无妨后点「继续」再邀请"
        await _set_entry(session, entry_id, state="manual_required", error_code="vacancy_not_safe_to_refill", error=error)
        return {"success": False, "partial": False, "status": "manual_required",
                "error_code": "vacancy_not_safe_to_refill", "error": error, "entry_id": entry_id,
                "vacancy": kicked.get("vacancy")}
    if removed:
        # 官方已移出就继续拉人；Sub2API 暂停失败之类的收尾问题只记在消息里。
        await onboard_service._progress(session, job_id=job_id, stage="kicked",
                                        message=str(kicked.get("message") or f"{replace_email} 已移出"))
        await session.commit()
        return None
    code = str(kicked.get("error_code") or "kick_failed")
    error = f"移出 {replace_email} 未完成：{kicked.get('error') or '踢人失败'}"
    manual = str(kicked.get("status") or "") in KICK_MANUAL_STATUSES
    await _set_entry(session, entry_id, state="manual_required" if manual else "failed", error_code=code[:60], error=error)
    return {"success": False, "partial": False, "status": "manual_required" if manual else "failed",
            "error_code": code, "error": error, "entry_id": entry_id}


async def _finish_joined(
    session: AsyncSession,
    result: dict[str, Any],
    *,
    entry_id: int,
    account_id: int,
    workspace_id: int,
    job_id: str,
    push_target: str = "sub2api",
) -> dict[str, Any]:
    from app.application import standby_pool
    from app.application.member_handoff import finish_after_authorization
    from app.application.onboard import onboard_service

    await onboard_service._progress(session, job_id=job_id, stage="pool_finish", message="正在推送并计数")
    await session.commit()
    followups = await finish_after_authorization(
        session, account_id, workspace_id=workspace_id, push_sub2api=True, count_switch=True,
        push_target=push_target,
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
    if push_target == "codex_rs":
        push, note = followups.get("codex_rs") or {}, CODEX_RS_FAILED_NOTE
    else:
        push, note = followups.get("sub2api") or {}, PUSH_FAILED_NOTE
    if push.get("ok") is False:
        message = f"{message}；{note}"
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
