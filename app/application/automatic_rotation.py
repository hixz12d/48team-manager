"""Automatic rotation completion: authorize, publish, and retry only publication."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select

from app.application.operations import operation_store
from app.application.sub2api_publish import account_sub2api_push
from app.core.time import as_utc, utcnow
from app.persistence.models.identity import Account, WorkspaceMembership
from app.persistence.models.operations import Operation

MAX_PUBLISH_ATTEMPTS = 5


async def preflight(service, db, workspace, child):
    """Check replacement dependencies and preserve the live role/seat before removing."""
    from app.application.reauth import load_cf_config
    from app.application.resources.hme import load_config
    from app.application.sub2api_defaults import load_defaults, validate_defaults
    from app.core.config import load_settings
    from app.integrations.openai.browser.environment import validate_configuration
    from app.integrations.openai.member_adapter import normalize_official_role

    validate_configuration(load_settings())
    if getattr(load_settings(), "browser_signup_flow", "legacy") == "extension":
        from app.integrations.openai.browser.signup import validate_signup_assets
        validate_signup_assets()
    owner = await db.get(Account, workspace.owner_account_id)
    if owner is None or not owner.proxy or not owner.access_token_encrypted:
        raise ValueError("母号凭据或代理未就绪")
    if not all((await load_cf_config(db)).values()):
        raise ValueError("验证码邮箱未配置")
    if not (await load_config(db)).configured:
        raise ValueError("HME 未配置")
    config = await service.sub2api.load_config(db)
    if not config.get("configured"):
        raise ValueError("Sub2API 未配置")
    await validate_defaults(db, await load_defaults(db))
    binding = await service._remote_binding_for(db, child, workspace_id=workspace.id)
    if binding.get("state") != "matched":
        raise ValueError("Sub2API 绑定或远端身份尚未确认")
    live, member = await service.workspaces.lookup_live_member(db, workspace, child.email)
    if not live.get("success") or not member or member.get("status") != "joined":
        raise ValueError("待轮转账号的官方成员身份未确认")
    role = normalize_official_role(member.get("role"))
    if role not in {"owner", "member"}:
        raise ValueError("原成员角色不支持自动补位")
    seat = {"prolite": "premium", "default": "standard"}.get(member.get("seat_type"))
    if seat is None:
        raise ValueError("原成员席位类型未确认，请先同步团队")
    return {"role": role, "seat_intent": seat}


async def publish_replacement(db, invite, workspace_id):
    """Never report usable capacity merely because registration or a write succeeded."""
    from app.application.sub2api_status import refresh, payloads

    child_id = (invite.get("child") or {}).get("id")
    account = await db.get(Account, child_id) if child_id else None
    if account is None or not invite.get("authorized"):
        return {**invite, "success": False, "partial": True, "error_code": "oauth_incomplete",
                "error": "补位账号授权未完成，保留同一账号等待处理"}
    revision = int(account.credential_revision or 1)
    pushed = {}
    written = bool(invite.get("publish_written"))
    receipt_ok = bool(invite.get("publish_receipt_ok"))
    try:
        if written:
            if not receipt_ok:
                from app.application.sub2api_remote_state import refresh_remote_state, retry_remote_followups
                current = await refresh_remote_state(db, child_id, workspace_id)
                latest = (current.get("snapshot") or {}).get("latest_operation") or {}
                if latest.get("operation_id") == invite.get("sub2api_operation_id"):
                    if current.get("can_retry"):
                        current = await retry_remote_followups(db, child_id, workspace_id)
                        latest = (current.get("snapshot") or {}).get("latest_operation") or {}
                    receipt_ok = bool(current.get("ok") and latest.get("state") == "completed")
        else:
            pushed = await account_sub2api_push(db, child_id, workspace_id=workspace_id)
            receipt_ok = bool(pushed.get("ok"))
            written = receipt_ok or pushed.get("credential_write") == "succeeded"
        await refresh(db, force=True)
        states, _ = await payloads(db)
        state = states.get((child_id, workspace_id), {})
        ready = bool(receipt_ok and not state.get("stale", True) and state.get("state") == "healthy")
    except Exception:
        await db.rollback()
        ready = False
    if ready:
        return {**invite, "success": True, "partial": False, "status": "success", "pushed": True,
                "error_code": None, "error": None, "publish_pending": False,
                "sub2api_operation_id": pushed.get("operation_id"),
                "message": "补位已完成授权，Sub2API 身份和可调度状态已确认"}
    return {**invite, "success": False, "partial": True, "status": "partial", "pushed": False,
            "error_code": "auto_publish_pending", "publish_pending": True,
            "publish_revision": revision, "publish_written": written, "publish_receipt_ok": receipt_ok,
            "sub2api_operation_id": pushed.get("operation_id") or invite.get("sub2api_operation_id"),
            "error": "补位账号已授权，Sub2API 尚未确认可用；将仅重试同步，不重复注册"}


async def refill_and_publish(service, db, *, seat_intent="workspace_default", **kwargs):
    invite = await service.onboard.refill(
        db, **kwargs, seat_intent=seat_intent, oauth_signup=True, use_phone_pool=True,
    )
    if not invite.get("success"):
        return invite
    parent = await operation_store.get_by_public_id(db, kwargs.get("job_id"))
    if parent is not None:
        await db.refresh(parent)
        if parent.cancel_requested:
            return {**invite, "success": False, "partial": True, "error_code": "cancel_after_side_effect",
                    "error": "补位已授权，任务已请求取消，未推送 Sub2API"}
        await operation_store.note(db, parent, "sub2api_push", "补位已授权，正在推送并核对 Sub2API")
        await db.commit()
    invite = await publish_replacement(db, invite, kwargs["workspace_id"])
    if invite.get("publish_pending"):
        invite.update(publish_attempts=1, publish_retry_at=(utcnow() + timedelta(minutes=1)).isoformat())
    return invite


async def retry_pending_publish(db, *, now=None, settings=None):
    """Durable, bounded retries of one known replacement; never re-run the rotation."""
    from app.application.rotate import rotate_service
    from app.domain.rotate import rotation_workspace_enabled
    cfg = settings if settings is not None else await rotate_service.load_settings(db)
    if not cfg.get("auto_rotate_enabled"):
        return {"retried": False}
    stamp = now or utcnow()
    rows = list(await db.scalars(select(Operation).where(
        Operation.op_type == "rotate", Operation.source == "auto", Operation.archived_at.is_(None),
        Operation.state == "partial", Operation.error_code == "auto_publish_pending",
    ).order_by(Operation.updated_at, Operation.id)))
    for parent in rows:
        if settings is None:
            cfg = await rotate_service.load_settings(db)
        if not rotation_workspace_enabled(cfg, parent.workspace_id):
            continue
        result = json.loads(parent.result_json or "{}")
        invite = result.get("invite") or {}
        retry_at = invite.get("publish_retry_at")
        if not retry_at or as_utc(datetime.fromisoformat(retry_at)) > stamp:
            continue
        if parent.cancel_requested:
            continue
        attempts = int(invite.get("publish_attempts") or 1)
        account_id = (invite.get("child") or {}).get("id")
        account = await db.get(Account, account_id) if account_id else None
        joined = await db.scalar(select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == parent.workspace_id,
            WorkspaceMembership.account_id == account_id, WorkspaceMembership.membership_state == "joined",
        ))
        if (attempts >= MAX_PUBLISH_ATTEMPTS or account is None or not joined
                or account.operational_state != "active" or account.auth_state != "healthy"
                or int(account.credential_revision or 1) != invite.get("publish_revision")):
            await operation_store.finish(db, parent, {**result, "success": False, "partial": False,
                "status": "manual_required", "error_code": "auto_publish_review",
                "error": "同步重试已达上限或补位账号发生变化，请核对同一账号"})
            await db.commit()
            continue
        lock, blocker = await operation_store.create_workspace_locked(
            db, op_type="rotate", workspace_id=parent.workspace_id, account_id=account_id,
            email=account.email, source="auto_sync", input_payload={"retry_of": parent.public_id},
        )
        if blocker:
            continue
        await db.commit()
        updated = await publish_replacement(db, invite, parent.workspace_id)
        await db.refresh(parent)
        await db.refresh(lock)
        updated.update(publish_attempts=attempts + 1,
                       publish_retry_at=(stamp + timedelta(minutes=2 ** attempts)).isoformat())
        completed = {**result, "invite": updated, "success": bool(updated.get("success")),
                     "partial": not updated.get("success"),
                     "status": "success" if updated.get("success") else "partial",
                     "error_code": updated.get("error_code"), "error": updated.get("error"),
                     "message": updated.get("message")}
        await operation_store.finish(db, lock, completed)
        await operation_store.finish(db, parent, completed)
        await db.commit()
        return {"retried": True, "operation_id": parent.public_id}
    return {"retried": False}


async def refresh_after_rotation(db, workspace_id):
    from app.application.jobs.workspace_sync import enqueue_workspace_sync
    from app.application.sub2api_status import refresh
    try:
        await enqueue_workspace_sync(db, workspace_id, source="automatic")
        await refresh(db, force=True)
    except Exception:
        # Mutation results are already durable; periodic readers can recover.
        await db.rollback()
