"""Console action services: operations, HME, accounts, proxy repair."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import verify_bindings
from app.application.onboard import onboard_service
from app.application.operations import operation_store, serialize_operation
from app.application.quota import quota_service
from app.application.reauth import reauth_service
from app.application.resources.hme import (
    ClaimedAlias,
    apply_local_label,
    reconcile_aliases,
    release_lease,
)
from app.application.resources.phones import phone_pool_service
from app.application.resources.proxies import proxy_profile_service
from app.application.rotate import rotate_service
from app.application.tokens import auth_service
from app.application.workspaces import workspace_service
from app.core.proxy import mask_proxy_url, normalize_proxy_url
from app.core.time import isoformat, utcnow
from app.domain.identity.ids import normalize_email
from app.domain.resources import (
    HME_STATE_RESERVED,
)
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account, Workspace
from app.persistence.models.operations import OperationStep
from app.persistence.models.resources import HmeAliasLease, ProxyProfile

SAFE_RETRY_TYPES = {"quota_probe", "auth_probe", "proxy_check", "workspace_sync", "hme_reconcile", "sub2api_sync", "sub2api_reconcile", "sub2api_push"}
UNSAFE_RETRY_TYPES = {"onboard", "rotate", "reauth", "free_register", "reregister", "free", "kick_member", "revoke_invite"}


def _mask_log_items(items: list[Any]) -> list[Any]:
    masked: list[Any] = []
    for item in items:
        if not isinstance(item, dict):
            masked.append(item)
            continue
        row = dict(item)
        for key in ("message", "error", "proxy", "url"):
            value = row.get(key)
            if isinstance(value, str) and "://" in value and "@" in value:
                row[key] = mask_proxy_url(value) or "***"
        masked.append(row)
    return masked


async def get_operation_detail(db: AsyncSession, public_id: str) -> dict[str, Any] | None:
    row = await operation_store.get_by_public_id(db, public_id)
    if row is None:
        return None
    steps = list(
        (
            await db.execute(
                select(OperationStep).where(OperationStep.operation_id == row.id).order_by(OperationStep.id.asc())
            )
        ).scalars()
    )
    payload = serialize_operation(row, steps=steps)
    payload["log"] = _mask_log_items(list(payload.get("log") or []))
    payload["can_cancel"] = row.state in {"queued", "running", "waiting"} and not row.cancel_requested
    payload["cancel_reason"] = (
        "already requested"
        if row.cancel_requested
        else ("" if payload["can_cancel"] else f"state={row.state} is not cancellable")
    )
    payload["can_retry"] = row.state in {"failed", "cancelled"} and row.op_type in SAFE_RETRY_TYPES
    if row.op_type in UNSAFE_RETRY_TYPES:
        payload["retry_reason"] = "destructive browser/workspace action cannot be blindly retried"
    elif not payload["can_retry"]:
        payload["retry_reason"] = f"type={row.op_type} state={row.state} is not in retry whitelist"
    else:
        payload["retry_reason"] = ""
    if row.resolved_proxy:
        payload["resolved_proxy"] = mask_proxy_url(row.resolved_proxy)
    return payload


async def request_operation_cancel(db: AsyncSession, public_id: str) -> dict[str, Any]:
    row = await operation_store.get_by_public_id(db, public_id)
    if row is None:
        return {"ok": False, "error": "operation not found", "error_code": "not_found"}
    if row.state not in {"queued", "running", "waiting"}:
        return {
            "ok": False,
            "error": f"cannot cancel operation in state={row.state}",
            "error_code": "not_cancellable",
            "operation_id": row.public_id,
        }
    row.cancel_requested = True
    if row.state == "queued":
        await operation_store.finish(
            db,
            row,
            {"success": False, "status": "cancelled", "error_code": "cancelled", "error": "cancelled before start"},
        )
        await db.commit()
        return {"ok": True, "operation_id": row.public_id, "cancel_requested": True, "state": "cancelled"}
    row.updated_at = utcnow()
    await operation_store.note(db, row, row.current_step or "cancel", "cancel requested", touch_lease=False)
    await db.commit()
    return {"ok": True, "operation_id": row.public_id, "cancel_requested": True}


async def retry_operation(db: AsyncSession, public_id: str) -> dict[str, Any]:
    row = await operation_store.get_by_public_id(db, public_id)
    if row is None:
        return {"ok": False, "error": "operation not found", "error_code": "not_found"}
    if row.op_type in UNSAFE_RETRY_TYPES:
        return {
            "ok": False,
            "error": "destructive browser/workspace action cannot be blindly retried",
            "error_code": "retry_forbidden",
            "operation_id": row.public_id,
        }
    if row.op_type not in SAFE_RETRY_TYPES or row.state not in {"failed", "cancelled"}:
        return {
            "ok": False,
            "error": f"type={row.op_type} state={row.state} is not retryable",
            "error_code": "retry_forbidden",
            "operation_id": row.public_id,
        }
    dispatched = await _dispatch_retry(db, row)
    if not dispatched.get("ok") and dispatched.get("error_code") in {"retry_unsupported", "not_found"}:
        return {"ok": False, "error": dispatched.get("error") or "retry failed", "error_code": dispatched.get("error_code") or "retry_unsupported", "operation_id": row.public_id}
    return {"ok": bool(dispatched.get("ok", True)), "operation_id": dispatched.get("operation_id"), "retry_of": row.public_id, **dispatched}


async def _dispatch_retry(db: AsyncSession, row) -> dict[str, Any]:
    op_type = row.op_type
    if op_type == "workspace_sync" and row.workspace_id:
        from app.application.workspace_sync import workspace_sync_service

        return await workspace_sync_service.sync_workspace(db, int(row.workspace_id))
    if op_type == "auth_probe" and row.account_id:
        return await account_auth_probe(db, int(row.account_id))
    if op_type == "quota_probe" and row.account_id:
        return await account_quota_probe(db, int(row.account_id))
    if op_type in {"sub2api_sync", "sub2api_reconcile"} and row.account_id:
        from app.application.sub2api_publish import account_sub2api_reconcile

        return await account_sub2api_reconcile(db, int(row.account_id))
    if op_type == "sub2api_push" and row.account_id:
        from app.application.sub2api_publish import account_sub2api_push

        return await account_sub2api_push(db, int(row.account_id))
    if op_type == "proxy_check" and row.resolved_proxy_profile_id:
        from app.application.resources.proxy_probe import proxy_probe_service

        return await proxy_probe_service.probe_profile(db, int(row.resolved_proxy_profile_id))
    if op_type in {"hme_reconcile", "reconcile"}:
        return await run_hme_reconcile(db)
    return {"ok": False, "error": f"no typed retry handler for {op_type}", "error_code": "retry_unsupported"}


async def run_hme_reconcile(db: AsyncSession) -> dict[str, Any]:
    operation = await operation_store.create(db, op_type="hme_reconcile", input_payload={"mode": "hme_readonly"})
    await operation_store.note(db, operation, "reconcile", "readonly HME reconcile")
    report = await reconcile_aliases(db)
    result = {"success": True, "status": "success", "readonly": True, **report}
    await operation_store.mark_step(db, operation, "reconcile", state="success", result=result)
    await operation_store.finish(db, operation, result)
    await db.commit()
    return {"ok": True, "operation_id": operation.public_id, **result}


async def retry_hme_label(db: AsyncSession, lease_id: int) -> dict[str, Any]:
    lease = await db.get(HmeAliasLease, int(lease_id))
    if lease is None:
        return {"ok": False, "error": "lease not found", "error_code": "not_found"}
    label = str(lease.label_desired or "").strip()
    if not label:
        return {"ok": False, "error": "desired label is empty", "error_code": "hme_label"}
    claimed = ClaimedAlias(
        email=lease.email,
        anonymous_id=lease.anonymous_id,
        account_id=lease.account_id,
        lease_id=lease.id,
        job_id=lease.job_id or "",
    )
    operation = await operation_store.create(
        db,
        op_type="hme_label_retry",
        email=lease.email,
        input_payload={"mode": "retry_label", "lease_id": lease.id, "label": label},
    )
    try:
        await apply_local_label(db, claimed, label)
        lease.label_sync_pending = False
        lease.last_error = None
        lease.updated_at = utcnow()
        result = {"success": True, "status": "success", "lease_id": lease.id, "label": label}
        await operation_store.mark_step(db, operation, "retry_label", state="success", result=result)
        await operation_store.finish(db, operation, result)
        await db.commit()
        return {"ok": True, "operation_id": operation.public_id, **result}
    except Exception as exc:
        message = str(exc)
        lease.last_error = message[:500]
        lease.label_sync_pending = True
        result = {"success": False, "status": "failed", "error": message, "error_code": "hme_label"}
        await operation_store.mark_step(
            db,
            operation,
            "retry_label",
            state="failed",
            error_code="hme_label",
            error_message=message,
        )
        await operation_store.finish(db, operation, result)
        await db.commit()
        return {"ok": False, "operation_id": operation.public_id, **result}


async def release_hme_lease_safe(db: AsyncSession, lease_id: int) -> dict[str, Any]:
    lease = await db.get(HmeAliasLease, int(lease_id))
    if lease is None:
        return {"ok": False, "error": "lease not found", "error_code": "not_found"}
    state = str(lease.local_state or HME_STATE_RESERVED)
    if state != HME_STATE_RESERVED or lease.label_sync_pending:
        return {
            "ok": False,
            "error": f"lease state={state} is not safely releasable",
            "error_code": "release_forbidden",
            "lease_id": lease.id,
        }
    claimed = ClaimedAlias(
        email=lease.email,
        anonymous_id=lease.anonymous_id,
        account_id=lease.account_id,
        lease_id=lease.id,
        job_id=lease.job_id or "",
    )
    await release_lease(db, claimed)
    return {"ok": True, "released": True, "lease_id": lease_id}


async def repair_proxy_profiles_from_accounts(db: AsyncSession) -> dict[str, Any]:
    dangling = list(
        (
            await db.execute(
                select(Account).where(
                    Account.proxy.is_not(None),
                    Account.proxy != "",
                    Account.proxy_profile_id.is_(None),
                )
            )
        ).scalars()
    )
    linked = 0
    for account in dangling:
        try:
            profile = await proxy_profile_service.upsert_from_url(
                db,
                str(account.proxy),
                name="",
                name_source="auto",
                bound_account=account,
            )
        except ValueError:
            continue
        account.proxy_profile_id = profile.id
        linked += 1
    if linked:
        await db.commit()
    return {"ok": True, "linked": linked}


async def account_auth_probe(db: AsyncSession, account_id: int) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    operation = await operation_store.create(
        db,
        op_type="auth_probe",
        account_id=account.id,
        email=account.email,
        input_payload={"account_id": account.id},
    )
    result = await auth_service.refresh_account(db, account)
    ok = bool(result.get("success"))
    payload = {
        "success": ok,
        "status": "success" if ok else "failed",
        "account_id": account.id,
        "auth_state": account.auth_state,
        "error": result.get("error"),
        "error_code": result.get("error_code"),
    }
    await operation_store.mark_step(
        db,
        operation,
        "auth_probe",
        state="success" if ok else "failed",
        result=payload,
        error_code=str(result.get("error_code") or ""),
        error_message=str(result.get("error") or ""),
    )
    await operation_store.finish(db, operation, payload)
    await db.commit()
    return {"ok": ok, "operation_id": operation.public_id, **payload}


async def account_quota_probe(db: AsyncSession, account_id: int, workspace_id: int | None = None) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    from app.domain.identity.binding import AmbiguousWorkspaceContext

    operation = await operation_store.create(
        db,
        op_type="quota_probe",
        account_id=account.id,
        email=account.email,
        workspace_id=workspace_id,
        input_payload={"account_id": account.id, "workspace_id": workspace_id},
    )
    try:
        snap = await quota_service.probe_account(db, account, workspace_id=workspace_id)
    except AmbiguousWorkspaceContext as exc:
        payload = {
            "ok": False,
            "success": False,
            "status": "failed",
            "error_code": "ambiguous_workspace_context",
            "error": str(exc),
            "message": "该账号属于多个 Workspace，请先选择要刷新额度的上下文",
            "account_id": account.id,
        }
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return payload
    ok = bool(getattr(snap, "success", False))
    payload = {
        "success": ok,
        "status": "success" if ok else "failed",
        "account_id": account.id,
        "five_hour_used_percent": getattr(snap, "five_hour_used_percent", None),
        "seven_day_used_percent": getattr(snap, "seven_day_used_percent", None),
        "queried_at": isoformat(getattr(snap, "queried_at", None)),
        "error": getattr(snap, "error_message", None) or getattr(snap, "error", None),
    }
    await operation_store.mark_step(
        db,
        operation,
        "quota_probe",
        state="success" if ok else "failed",
        result=payload,
        error_message=str(payload.get("error") or ""),
    )
    await operation_store.finish(db, operation, payload)
    await db.commit()
    return {"ok": ok, "operation_id": operation.public_id, **payload}


async def account_reauth(db: AsyncSession, account_id: int) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    return await reauth_service.start_manual_reauth(db, account)


async def account_reauth_complete(
    db: AsyncSession,
    account_id: int,
    *,
    ticket: str,
    callback_url: str,
) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    return await reauth_service.complete_manual_reauth(
        db,
        account,
        ticket=ticket,
        callback_url=callback_url,
    )


async def account_sub2api_sync(db: AsyncSession, account_id: int) -> dict[str, Any]:
    from app.application.sub2api_publish import account_sub2api_reconcile

    return await account_sub2api_reconcile(db, account_id)


async def account_sub2api_reconcile(db: AsyncSession, account_id: int) -> dict[str, Any]:
    from app.application.sub2api_publish import account_sub2api_reconcile as _reconcile

    return await _reconcile(db, account_id)


async def account_sub2api_push(
    db: AsyncSession,
    account_id: int,
    *,
    group_ids: list[int] | None = None,
    name: str | None = None,
    schedulable: bool | None = True,
    confirm_mixed_channel_risk: bool = False,
    workspace_id: int | None = None,
) -> dict[str, Any]:
    from app.application.sub2api_publish import account_sub2api_push as _push

    return await _push(
        db,
        account_id,
        group_ids=group_ids,
        name=name,
        schedulable=schedulable,
        confirm_mixed_channel_risk=confirm_mixed_channel_risk,
        workspace_id=workspace_id,
    )


async def account_refresh(db: AsyncSession, account_id: int) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    operation = await operation_store.create(
        db,
        op_type="auth_probe",
        account_id=account.id,
        email=account.email,
        input_payload={"account_id": account.id, "mode": "refresh_bundle"},
    )
    steps: dict[str, Any] = {}

    auth = await auth_service.refresh_account(db, account)
    steps["auth"] = {"ok": bool(auth.get("success")), "error": auth.get("error"), "auth_state": account.auth_state}
    await operation_store.mark_step(
        db,
        operation,
        "auth",
        state="success" if steps["auth"]["ok"] else "failed",
        result=steps["auth"],
        error_message=str(auth.get("error") or ""),
    )

    try:
        snap = await quota_service.probe_account(db, account)
        steps["quota"] = {
            "ok": bool(getattr(snap, "success", False)),
            "seven_day_used_percent": getattr(snap, "seven_day_used_percent", None),
            "error": getattr(snap, "error_message", None) or getattr(snap, "error", None),
        }
    except Exception as exc:
        steps["quota"] = {"ok": False, "error": str(exc)}
    await operation_store.mark_step(
        db,
        operation,
        "quota",
        state="success" if steps["quota"]["ok"] else "failed",
        result=steps["quota"],
        error_message=str(steps["quota"].get("error") or ""),
    )

    try:
        remotes = await sub2api_client.list_status_accounts(db)
        report = await verify_bindings(db, remotes)
        steps["sub2api"] = {"ok": True, "remote_count": len(remotes or []), "report_keys": sorted(list(report.keys()))[:8]}
    except Exception as exc:
        steps["sub2api"] = {"ok": False, "error": str(exc)}
    await operation_store.mark_step(
        db,
        operation,
        "sub2api",
        state="success" if steps["sub2api"]["ok"] else "failed",
        result=steps["sub2api"],
        error_message=str(steps["sub2api"].get("error") or ""),
    )

    all_ok = all(item.get("ok") for item in steps.values())
    any_ok = any(item.get("ok") for item in steps.values())
    if all_ok:
        status = "success"
        ok = True
        partial = False
    elif any_ok:
        status = "partial"
        ok = False
        partial = True
    else:
        status = "failed"
        ok = False
        partial = False
    payload = {
        "success": all_ok,
        "ok": ok,
        "status": status,
        "partial": partial,
        "steps": steps,
        "account_id": account.id,
        "message": "状态刷新完成" if all_ok else ("部分步骤失败" if partial else "状态刷新失败"),
    }
    await operation_store.finish(db, operation, payload)
    await db.commit()
    return {"ok": ok, "operation_id": operation.public_id, **payload}

def _ok_result(result: dict[str, Any], *, operation_id: str | None = None) -> dict[str, Any]:
    payload = dict(result or {})
    success = bool(payload.get("success") if "success" in payload else payload.get("ok"))
    if operation_id and "operation_id" not in payload:
        payload["operation_id"] = operation_id
    payload["ok"] = success
    return payload


async def start_workspace_onboard(
    db: AsyncSession,
    workspace_id: int,
    *,
    email_line: str = "",
    phone_line: str = "",
    proxy: str = "",
    password: str = "",
    force: bool = False,
    skip_invite: bool = False,
) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    operation = await operation_store.create(
        db,
        op_type="onboard",
        workspace_id=workspace.id,
        email=str(email_line or "").strip(),
        phone=str(phone_line or "").strip(),
        input_payload={
            "workspace_id": workspace.id,
            "email_line": email_line,
            "phone_line": phone_line,
            "proxy": mask_proxy_url(proxy) if proxy else "",
            "force": bool(force),
            "skip_invite": bool(skip_invite),
        },
        resolved_proxy=str(proxy or "").strip(),
    )
    await db.commit()
    result = await onboard_service.invite_and_onboard(
        db,
        workspace_id=workspace.id,
        email_line=email_line,
        phone_line=phone_line,
        proxy=proxy,
        password=password,
        force=force,
        skip_invite=skip_invite,
        job_id=operation.public_id,
        in_test=False,
    )
    await operation_store.finish(db, operation, result)
    await db.commit()
    return _ok_result(result, operation_id=operation.public_id)


async def start_controlled_rotate(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str,
    email_line: str = "",
    phone_line: str = "",
    proxy: str = "",
    force_refill: bool = False,
    reason: str = "console",
) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    target = normalize_email(email)
    if not target:
        return {"ok": False, "error": "email required", "error_code": "email_required"}
    child = (
        await db.execute(select(Account).where(Account.email == normalize_email(target)))
    ).scalar_one_or_none()
    operation = await operation_store.create(
        db,
        op_type="rotate",
        workspace_id=workspace.id,
        account_id=child.id if child else None,
        email=target,
        phone=str(phone_line or "").strip(),
        input_payload={
            "workspace_id": workspace.id,
            "email": target,
            "force_refill": bool(force_refill),
            "reason": reason,
            "source": "manual",
            "confirmed": True,
        },
        source="manual",
    )
    await db.commit()
    result = await rotate_service.run_rotate_saga(
        db,
        job_id=operation.public_id,
        workspace_id=workspace.id,
        email=target,
        reason=reason or "console",
        force_refill=force_refill,
        email_line=email_line,
        phone_line=phone_line,
        proxy=proxy,
        child_id=child.id if child else None,
        skip_confirm=True,
        in_test=False,
    )
    await operation_store.finish(db, operation, result)
    await db.commit()
    return _ok_result(result, operation_id=operation.public_id)


async def kick_member_to_standby(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str,
    user_id: str | None = None,
    reason: str = "console_kick",
    unbind_sub2api: bool = False,
) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    target = normalize_email(email)
    if not target:
        return {"ok": False, "error": "email required", "error_code": "email_required"}
    child = (
        await db.execute(select(Account).where(Account.email == normalize_email(target)))
    ).scalar_one_or_none()
    operation = await operation_store.create(
        db,
        op_type="kick_member",
        workspace_id=workspace.id,
        account_id=child.id if child else None,
        email=target,
        input_payload={
            "workspace_id": workspace.id,
            "email": target,
            "user_id": user_id,
            "reason": reason,
            "mode": "kick_only",
            "unbind_sub2api": bool(unbind_sub2api),
        },
    )
    await db.commit()
    result = await rotate_service.kick_to_standby(
        db,
        workspace_id=workspace.id,
        email=target,
        user_id=user_id,
        reason=reason,
        unbind_sub2api=unbind_sub2api,
        job_id=operation.public_id,
    )
    await operation_store.finish(db, operation, result)
    await db.commit()
    return _ok_result(result, operation_id=operation.public_id)


async def revoke_workspace_invite(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str,
) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    target = normalize_email(email)
    if not target:
        return {"ok": False, "error": "email required", "error_code": "email_required"}
    operation = await operation_store.create(
        db,
        op_type="revoke_invite",
        workspace_id=workspace.id,
        email=target,
        input_payload={"workspace_id": workspace.id, "email": target, "mode": "revoke_invite"},
    )
    await db.commit()
    result = await workspace_service.revoke_invite(db, workspace.id, target)
    await operation_store.finish(db, operation, result)
    await db.commit()
    return _ok_result(result, operation_id=operation.public_id)


async def update_account_proxy(
    db: AsyncSession,
    account_id: int,
    *,
    proxy: str | None = None,
    proxy_profile_id: int | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    if account.local_purpose != "mother":
        return {"ok": False, "error": "only mother accounts can change proxy here", "error_code": "not_mother"}

    if clear:
        account.proxy = None
        account.proxy_profile_id = None
    elif proxy_profile_id is not None:
        profile = await db.get(ProxyProfile, int(proxy_profile_id))
        if profile is None:
            return {"ok": False, "error": "proxy profile not found", "error_code": "proxy_not_found"}
        account.proxy = proxy_profile_service.compose(profile)
        account.proxy_profile_id = profile.id
    else:
        raw = str(proxy or "").strip()
        if not raw:
            return {"ok": False, "error": "proxy required", "error_code": "proxy_required"}
        try:
            url = normalize_proxy_url(raw)
        except ValueError as exc:
            return {"ok": False, "error": str(exc), "error_code": "invalid_proxy"}
        if not url:
            return {"ok": False, "error": "proxy required", "error_code": "proxy_required"}
        from app.domain.resources.proxy_names import default_name_for_account, NAME_SOURCE_AUTO

        auto_name = default_name_for_account(purpose=account.local_purpose, email=account.email)
        profile = await proxy_profile_service.upsert_from_url(
            db,
            url,
            name=auto_name,
            name_source=NAME_SOURCE_AUTO,
            bound_account=account,
        )
        account.proxy = url
        account.proxy_profile_id = profile.id

    account.updated_at = utcnow()
    from app.integrations.openai.chatgpt import chatgpt_client

    await chatgpt_client.clear_session(account.email)
    await db.commit()
    return {
        "ok": True,
        "account_id": account.id,
        "proxy": "set" if account.proxy else "none",
        "proxy_url": mask_proxy_url(account.proxy) if account.proxy else None,
        "proxy_profile_id": account.proxy_profile_id,
    }


async def set_phone_status(db: AsyncSession, phone_id: int, status: str) -> dict[str, Any]:
    return await phone_pool_service.set_status(db, phone_id, status)


async def reset_phone_cooldown(db: AsyncSession, phone_id: int) -> dict[str, Any]:
    return await phone_pool_service.reset_cooldown(db, phone_id)


async def proxy_bindings(db: AsyncSession, proxy_id: int) -> dict[str, Any]:
    return await proxy_profile_service.list_bindings(db, proxy_id)


# Re-export maintenance helpers so routes can keep importing console_actions.
from app.application.console_maintenance import (  # noqa: E402
    archive_operation,
    bulk_archive_operations,
    link_remote_only_member,
    add_local_child,
    remove_local_child,
    repair_workspace_names,
    sync_workspace_official_name,
    restore_operation,
    update_workspace_display_name,
)
