"""Console action services: operations, HME, accounts, proxy repair."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import verify_bindings
from app.application.onboard import onboard_service
from app.application.replenish import replenish_service
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
from app.application.proxy_resolution import ProxyResolutionError, resolve_sub2api_proxy
from app.application.rotate import rotate_service
from app.application.tokens import auth_service
from app.application.workspaces import workspace_service
from app.core.proxy import mask_proxy_url, normalize_proxy_url
from app.core.time import isoformat, utcnow
from app.domain.identity.ids import normalize_email
from app.domain.quota import quota_probe_user_message
from app.integrations.openai.member_adapter import parse_invite_role, parse_invite_seat_intent
from app.domain.resources import (
    HME_STATE_RESERVED,
)
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account, Workspace
from app.persistence.models.operations import OperationStep
from app.persistence.models.resources import HmeAliasLease, ProxyProfile

SAFE_RETRY_TYPES = {
    "quota_probe",
    "auth_probe",
    "proxy_check",
    "workspace_sync",
    "hme_reconcile",
    "sub2api_sync",
    "sub2api_reconcile",
    "sub2api_push",
    "sub2api_usage_sync",
}
UNSAFE_RETRY_TYPES = {
    "onboard",
    "replenish",
    "rotate",
    "reauth",
    "free_register",
    "reregister",
    "free",
    "kick_member",
    "purge_child",
    "revoke_invite",
    "invite_child",
    "update_member_role",
    "pool_join",
}


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
    from app.application.manual_rotation import can_continue

    payload["can_continue_rotation"] = can_continue(row)
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
        from app.application.jobs.workspace_sync import enqueue_workspace_sync

        return await enqueue_workspace_sync(db, int(row.workspace_id), source="retry")
    if op_type == "auth_probe" and row.account_id:
        return await account_auth_probe(db, int(row.account_id))
    if op_type == "quota_probe" and row.account_id:
        return await account_quota_probe(db, int(row.account_id), workspace_id=row.workspace_id)
    if op_type in {"sub2api_sync", "sub2api_reconcile"} and row.account_id:
        from app.application.sub2api_publish import account_sub2api_reconcile

        return await account_sub2api_reconcile(db, int(row.account_id))
    if op_type == "sub2api_push" and row.account_id:
        from app.application.sub2api_publish import account_sub2api_push

        return await account_sub2api_push(db, int(row.account_id))
    if op_type == "sub2api_usage_sync":
        from app.application.sub2api_usage import sub2api_usage_service

        return await sub2api_usage_service.sync(
            db,
            workspace_id=row.workspace_id,
            account_id=row.account_id,
            source="retry",
        )
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


async def register_local_account(db: AsyncSession, *, email: str, purpose: str) -> dict[str, Any]:
    from sqlalchemy.dialects.sqlite import insert
    from app.domain.quota_health import present_context
    created = await db.execute(insert(Account).values(email=email, local_purpose=purpose,
        operational_state="available", auth_state="unknown").on_conflict_do_nothing(index_elements=["email"]).returning(Account.id))
    account_id = created.scalar_one_or_none()
    if account_id is None:
        await db.rollback()
        return {"ok": False, "error_code": "account_exists", "error": "账号已登记，请在列表中打开现有档案"}
    await db.commit()
    account = await db.get(Account, account_id)
    return {"ok": True, "account": {"id": account.id, "email": account.email,
        "purpose": account.local_purpose, "state": account.operational_state, "kind": "unassigned",
        "has_access_token": False, **present_context(account)}}


async def account_quota_probe(db: AsyncSession, account_id: int, workspace_id: int | None = None) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    from app.domain.identity.binding import AmbiguousWorkspaceContext
    try:
        return await quota_service.enqueue(db, account, workspace_id)
    except AmbiguousWorkspaceContext as exc:
        return {"ok": False, "error_code": exc.error_code, "error": str(exc)}
    except ValueError as exc:
        return {"ok": False, "error_code": "invalid_workspace_context", "error": str(exc)}


async def account_reauth(db: AsyncSession, account_id: int) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "account_not_found"}
    return await reauth_service.start_manual_reauth(db, account)


async def bind_account_phone(db: AsyncSession, account_id: int, phone_line: str) -> dict[str, Any]:
    from app.integrations.sms.client import parse_phone_line

    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    number, sms_url = parse_phone_line(phone_line)
    if not number or not sms_url:
        return {"ok": False, "error": "手机号格式应为 +1xxxxxxxxxx----https://...", "error_code": "sms_missing"}
    account.phone = number
    account.sms_url = sms_url
    await db.commit()
    return {"ok": True, "account_id": account.id, "email": account.email, "phone": number}


async def start_account_immediate_reauth(db: AsyncSession, account_id: int) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    busy = await operation_store.browser_busy(db)
    if busy is not None:
        return {
            "ok": False,
            "success": False,
            "error": f"已有浏览器任务 {busy.email or busy.public_id}",
            "error_code": "browser_busy",
            "operation_id": busy.public_id,
        }
    operation = await operation_store.create(
        db,
        op_type="reauth",
        account_id=account.id,
        email=account.email,
        input_payload={"account_id": account.id, "email": account.email, "mode": "immediate"},
        source="manual",
    )
    await db.commit()
    result = await reauth_service.run_immediate_reauth(
        db,
        account,
        progress_job_id=operation.public_id,
        skip_browser_busy=True,
    )
    await operation_store.finish(db, operation, result)
    await db.commit()
    return _ok_result(result, operation_id=operation.public_id)


async def account_reauth_complete(
    db: AsyncSession,
    account_id: int,
    *,
    ticket: str,
    callback_url: str,
    workspace_id: int | None = None,
    push_sub2api: bool = False,
    count_switch: bool = False,
) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "account_not_found"}
    result = await reauth_service.complete_manual_reauth(
        db,
        account,
        ticket=ticket,
        callback_url=callback_url,
    )
    if result.get("ok") and (push_sub2api or count_switch):
        from app.application.member_handoff import finish_after_authorization

        result["followups"] = await finish_after_authorization(
            db,
            account.id,
            workspace_id=workspace_id,
            token_sync=result.get("sub2api_token_sync"),
            push_sub2api=push_sub2api,
            count_switch=count_switch,
        )
    if result.get("ok"):
        from app.application import manual_rotation

        pending = await manual_rotation.unresolved_for_replacement(db, account.email)
        if pending is not None:
            # The rotation stopped only for this authorization; finish it without another click.
            resumed = await continue_manual_rotation(db, pending.public_id, background=True)
            result["rotation_continue"] = {
                "ok": bool(resumed.get("ok")), "operation_id": pending.public_id,
                "error": resumed.get("error"),
                "message": "已自动继续原轮转（推送、清理旧号、计数）" if resumed.get("ok")
                else f"原轮转未能自动继续：{resumed.get('error') or '请到任务里点继续轮转'}",
            }
    return result


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
    schedulable: bool | None = None,
    confirm_mixed_channel_risk: bool = False,
    workspace_id: int | None = None,
    dry_run: bool = False,
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
        dry_run=dry_run,
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


async def _run_command(db: AsyncSession, operation, command, *, background: bool) -> dict[str, Any]:
    """Run a locked workspace command inline, or hand it to the runner and return its id.

    The command gets its own session in background mode, so it must only close
    over plain values (ids, strings), never ORM rows from the request session.
    """
    from app.application.jobs.commands import command_runner

    if background and command_runner.ready:
        command_runner.spawn(operation.public_id, command)
        return {
            "ok": True,
            "accepted": True,
            "status": "running",
            "state": "running",
            "operation": operation.op_type,
            "operation_id": operation.public_id,
            "workspace_id": operation.workspace_id,
            "account_id": operation.account_id,
            "email": operation.email or "",
        }
    result = await command(db)
    await operation_store.finish(db, operation, result)
    await db.commit()
    return _ok_result(result, operation_id=operation.public_id)


async def start_workspace_onboard(
    db: AsyncSession,
    workspace_id: int,
    *,
    email_line: str = "",
    phone_line: str = "",
    proxy: str = "",
    proxy_selection: dict[str, Any] | None = None,
    password: str = "",
    force: bool = False,
    skip_invite: bool = False,
    role: str = "owner",
    seat_intent: str = "workspace_default",
    oauth_signup: bool = False,
    browser_executable: str = "",
    background: bool = False,
) -> dict[str, Any]:
    seat_intent = parse_invite_seat_intent(seat_intent).value
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    if proxy and proxy_selection:
        return {"ok": False, "error": "choose exactly one proxy source", "error_code": "proxy_choice_conflict"}

    proxy_value = str(proxy or "").strip()
    proxy_source = "legacy" if proxy_value else ""
    sub2api_proxy_id = None
    proxy_instance_key = ""
    if proxy_selection is not None:
        if str(proxy_selection.get("source") or "") != "sub2api":
            return {"ok": False, "error": "unsupported proxy source", "error_code": "invalid_proxy_source"}
        try:
            resolved = await resolve_sub2api_proxy(db, int(proxy_selection.get("remote_id") or 0))
        except ProxyResolutionError as exc:
            return {"ok": False, "error": str(exc), "error_code": exc.error_code}
        except Exception:
            return {"ok": False, "error": "Sub2API proxy catalog unavailable", "error_code": "remote_catalog_unavailable"}
        proxy_value = resolved.url
        proxy_source = resolved.source
        sub2api_proxy_id = resolved.remote_id
        proxy_instance_key = resolved.instance_key

    operation, blocker = await operation_store.create_workspace_locked(
        db,
        op_type="onboard",
        workspace_id=workspace.id,
        email=str(email_line or "").strip(),
        phone="" if oauth_signup else str(phone_line or "").split("----", 1)[0].strip(),
        input_payload={
            "workspace_id": workspace.id,
            "email_line": email_line,
            "phone_line": phone_line,
            "proxy_source": proxy_source or None,
            "sub2api_proxy_id": sub2api_proxy_id,
            "force": bool(force),
            "skip_invite": bool(skip_invite),
            "seat_intent": seat_intent,
            "requested_role": parse_invite_role(role),
            "oauth_signup": bool(oauth_signup),
        },
        resolved_proxy=proxy_value,
    )
    if blocker is not None:
        return {"ok": False, "error_code": "operation_conflict",
                "error": "工作区已有变更任务", "operation_id": blocker.public_id}
    await db.commit()
    target_id, job_id = workspace.id, operation.public_id

    async def command(session: AsyncSession) -> dict[str, Any]:
        return await onboard_service.invite_and_onboard(
            session,
            workspace_id=target_id,
            email_line=email_line,
            phone_line=phone_line,
            proxy=proxy_value,
            proxy_source=proxy_source,
            sub2api_proxy_id=sub2api_proxy_id,
            proxy_instance_key=proxy_instance_key,
            password=password,
            force=force,
            skip_invite=skip_invite,
            role=role,
            seat_intent=seat_intent,
            job_id=job_id,
            in_test=False,
            oauth_signup=oauth_signup,
            browser_executable=browser_executable,
        )

    return await _run_command(db, operation, command, background=background)


async def start_workspace_replenish(
    db: AsyncSession,
    workspace_id: int,
    *,
    role: str = "owner",
    phone_line: str = "",
    seat_intent: str = "workspace_default",
    background: bool = False,
) -> dict[str, Any]:
    seat_intent = parse_invite_seat_intent(seat_intent).value
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    operation, blocker = await operation_store.create_workspace_locked(
        db,
        op_type="replenish",
        workspace_id=workspace.id,
        phone="",
        input_payload={
            "workspace_id": workspace.id,
            "requested_role": parse_invite_role(role),
            "mode": "replenish_one",
            "seat_intent": seat_intent,
            "phone_line": phone_line,
        },
    )
    if blocker is not None:
        return {"ok": False, "error_code": "operation_conflict",
                "error": "工作区已有变更任务", "operation_id": blocker.public_id}
    await db.commit()
    target_id, job_id = workspace.id, operation.public_id

    async def command(session: AsyncSession) -> dict[str, Any]:
        return await replenish_service.run(
            session,
            workspace_id=target_id,
            job_id=job_id,
            role=role,
            phone_line=phone_line,
            seat_intent=seat_intent,
            in_test=False,
        )

    return await _run_command(db, operation, command, background=background)


async def start_controlled_rotate(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str,
    replacement_email: str = "",
    confirm_vacancy: bool = False,
    background: bool = False,
) -> dict[str, Any]:
    """Manual one-for-one rotation; role and seat are read live from the old member."""
    from app.application import manual_rotation

    operation, blocked = await manual_rotation.open_rotation(
        db, workspace_id, email=email, replacement_email=replacement_email,
    )
    if blocked is not None:
        return blocked
    job_id = operation.public_id

    async def command(session: AsyncSession) -> dict[str, Any]:
        return await manual_rotation.run_manual_rotation(session, job_id, confirm_vacancy=confirm_vacancy)

    return await _run_command(db, operation, command, background=background)


SELFCHECK_OP_TYPE = "runner_selfcheck"
_SELFCHECK_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SELFCHECK_SHOT = re.compile(r"^[a-z0-9_-]{1,40}\.png$")
_SELFCHECK_LABELS = {"browserscan": "BrowserScan", "creepjs": "CreepJS", "browserleaks_webrtc": "BrowserLeaks WebRTC"}


def _runner_config_view() -> dict[str, Any]:
    """Read-only runner configuration for the settings card; never exposes paths beyond set / exists."""
    from pathlib import Path

    from app.application.extension_runner import validate_runner_configuration
    from app.core.config import load_settings
    from app.integrations.openai.browser.environment import BrowserEnvironmentError

    settings = load_settings()
    executable = str(settings.runner_browser_executable or "").strip()
    try:
        validate_runner_configuration()
        config_error, config_code = "", ""
    except BrowserEnvironmentError as exc:
        config_error, config_code = str(exc), getattr(exc, "error_code", "runner_not_configured")
    return {
        "signup_runner": settings.rotation_signup_runner,
        "executable_set": bool(executable),
        "executable_exists": bool(executable) and Path(executable).is_file(),
        "platform": settings.runner_fingerprint_platform,
        "gpu_mode": settings.runner_gpu_mode,
        "ready": not config_error,
        "error": config_error,
        "error_code": config_code,
    }


async def _selfcheck_workspaces(db: AsyncSession) -> list[dict[str, Any]]:
    rows = list(await db.scalars(select(Workspace).where(Workspace.status == "active").order_by(Workspace.id)))
    owner_ids = {row.owner_account_id for row in rows if row.owner_account_id}
    owners = {item.id: item for item in await db.scalars(select(Account).where(Account.id.in_(owner_ids)))} if owner_ids else {}
    items = []
    for row in rows:
        owner = owners.get(row.owner_account_id)
        items.append({
            "id": row.id,
            "name": row.custom_name or row.official_name or row.name or f"工作区 #{row.id}",
            "proxy_set": bool(owner and str(owner.proxy or "").strip()),
        })
    return items


def _selfcheck_shots(public_id: str, paths: list[Any]) -> list[dict[str, str]]:
    shots = []
    for raw in paths or []:
        name = str(raw or "").rsplit("/", 1)[-1]
        if not _SELFCHECK_SHOT.fullmatch(name):
            continue
        stem = name[:-4]
        shots.append({"name": stem, "label": _SELFCHECK_LABELS.get(stem, stem),
                      "url": f"/api/runner/selfcheck/{public_id}/screenshots/{name}"})
    return shots


def _selfcheck_diagnostics(diagnostics: Any) -> dict[str, Any]:
    """Keep the operation row small: summary fields, not the full plugin report."""
    data = diagnostics if isinstance(diagnostics, dict) else {}
    plugin = data.get("plugin") if isinstance(data.get("plugin"), dict) else {}
    return {
        "duration_seconds": data.get("duration_seconds"),
        "last": data.get("last"),
        "event_count": data.get("event_count"),
        "plugin_version": plugin.get("version"),
        "browser_log": list(data.get("browser_log") or [])[-20:],
    }


def _selfcheck_result(public_id: str, workspace_name: str, outcome: dict[str, Any]) -> dict[str, Any]:
    findings = [item for item in outcome.get("findings") or [] if isinstance(item, dict)]
    errors = sum(1 for item in findings if item.get("level") == "error")
    warnings = len(findings) - errors
    completed = bool(outcome.get("completed"))
    error_code = str(outcome.get("error_code") or "")
    # The task succeeds when the probe ran to the end; environment problems are reported as findings.
    success = completed and not error_code
    if not success:
        message = f"自检未完成：{outcome.get('error') or error_code or '插件没有报告完成'}"
    elif errors or warnings:
        message = f"自检完成：{errors} 个问题、{warnings} 个提醒"
    else:
        message = "自检完成：未发现问题"
    return {
        "success": success,
        "ok": success,
        "status": "success" if success else "failed",
        "passed": bool(outcome.get("ok")),
        "completed": completed,
        "error_code": error_code,
        "error": str(outcome.get("error") or "") if not success else "",
        "message": message,
        "workspace_name": workspace_name,
        "summary": outcome.get("summary") or "",
        "platform": outcome.get("platform") or "",
        "exit": outcome.get("exit"),
        "signals": outcome.get("signals"),
        "findings": findings,
        "screenshots": _selfcheck_shots(public_id, outcome.get("screenshots") or []),
        "probe_status": outcome.get("probe_status") or {},
        "diagnostics": _selfcheck_diagnostics(outcome.get("diagnostics")),
    }


async def runner_selfcheck_overview(db: AsyncSession) -> dict[str, Any]:
    """Settings card: runner configuration, teams whose mother proxy can be used, last self-check."""
    from app.persistence.models.operations import Operation

    latest = await db.scalar(select(Operation).where(Operation.op_type == SELFCHECK_OP_TYPE)
                             .order_by(Operation.id.desc()).limit(1))
    return {
        "config": _runner_config_view(),
        "workspaces": await _selfcheck_workspaces(db),
        "latest": serialize_operation(latest) if latest is not None else None,
    }


async def start_runner_selfcheck(
    db: AsyncSession,
    *,
    platform: str | None,
    workspace_id: int | None = None,
    background: bool = False,
) -> dict[str, Any]:
    """Launch Chromix + the extension against fingerprint pages. No signup, no official team change."""
    from app.application.extension_runner import check_runner_proxy, validate_runner_configuration
    from app.integrations.openai.browser.environment import BrowserEnvironmentError

    chosen = str(platform or "").strip().lower() or None
    if chosen not in {None, "linux", "windows"}:
        return {"ok": False, "error_code": "invalid_platform", "error": "伪装平台只能是 linux 或 windows"}
    try:
        validate_runner_configuration()
    except BrowserEnvironmentError as exc:
        return {"ok": False, "error_code": getattr(exc, "error_code", "runner_not_configured"), "error": str(exc)}

    if workspace_id:
        workspace = await db.get(Workspace, int(workspace_id))
        if workspace is None or workspace.status != "active":
            return {"ok": False, "error_code": "not_found", "error": "团队不存在或未启用"}
    else:
        candidates = [item for item in await _selfcheck_workspaces(db) if item["proxy_set"]]
        workspace = await db.get(Workspace, candidates[0]["id"]) if candidates else None
        if workspace is None:
            return {"ok": False, "error_code": "proxy_missing", "error": "没有已配置母号代理的启用团队"}
    owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
    proxy_url = str(owner.proxy or "").strip() if owner else ""
    try:
        await check_runner_proxy(proxy_url)
    except BrowserEnvironmentError as exc:
        return {"ok": False, "error_code": getattr(exc, "error_code", "proxy_missing"), "error": str(exc)}
    busy = await operation_store.browser_busy(db)
    if busy is not None:
        return {"ok": False, "error_code": "browser_busy", "error": "浏览器正在执行其他任务，请等它结束后再自检",
                "operation_id": busy.public_id}

    workspace_name = workspace.custom_name or workspace.official_name or workspace.name or f"工作区 #{workspace.id}"
    operation = await operation_store.create(
        db,
        op_type=SELFCHECK_OP_TYPE,
        workspace_id=workspace.id,
        input_payload={"workspace_id": workspace.id, "platform": chosen},
        resolved_proxy=proxy_url,
    )
    await db.commit()
    job_id = operation.public_id

    async def command(session: AsyncSession) -> dict[str, Any]:
        from app.application.extension_runner import run_selfcheck
        from app.application.jobs.browser import InvitedBrowserSession

        slot = InvitedBrowserSession()
        try:
            if not await slot.try_reserve():
                return {"success": False, "status": "failed", "error_code": "browser_busy",
                        "error": "浏览器正在执行其他任务，请等它结束后再自检"}
            outcome = await run_selfcheck(session, proxy_url=proxy_url, platform=chosen, job_id=job_id)
        finally:
            await slot.close()
        return _selfcheck_result(job_id, workspace_name, outcome)

    return await _run_command(db, operation, command, background=background)


def selfcheck_screenshot_path(public_id: str, name: str):
    """Only ``data/selfcheck/<public_id>/<[a-z0-9_-]+>.png``; ``None`` for anything else."""
    from app.application.extension_runner import SELFCHECK_ROOT

    if not _SELFCHECK_ID.fullmatch(str(public_id or "")) or not _SELFCHECK_SHOT.fullmatch(str(name or "")):
        return None
    root = SELFCHECK_ROOT.resolve()
    path = (root / public_id / name).resolve()
    if path.parent.parent != root or not path.is_file():
        return None
    return path


async def recover_runner_selfchecks(db: AsyncSession) -> int:
    """A self-check only runs inside this process; after a restart it can never finish."""
    from app.domain.automation import ACTIVE_STATES
    from app.persistence.models.operations import Operation

    rows = list(await db.scalars(select(Operation).where(
        Operation.op_type == SELFCHECK_OP_TYPE, Operation.state.in_(ACTIVE_STATES),
    )))
    for row in rows:
        await operation_store.finish(db, row, {
            "success": False, "status": "failed", "error_code": "runner_interrupted",
            "error": "服务重启，自检中断；可重新发起，不影响任何团队",
        })
    if rows:
        await db.commit()
    return len(rows)


async def continue_manual_rotation(
    db: AsyncSession,
    public_id: str,
    *,
    confirm_vacancy: bool = False,
    background: bool = False,
) -> dict[str, Any]:
    """Resume only the unconfirmed stages of one manual rotation; never restart it."""
    from app.application import manual_rotation

    operation, blocked = await manual_rotation.reopen_rotation(db, public_id)
    if blocked is not None:
        return blocked
    job_id = operation.public_id

    async def command(session: AsyncSession) -> dict[str, Any]:
        return await manual_rotation.run_manual_rotation(session, job_id, confirm_vacancy=confirm_vacancy)

    return await _run_command(db, operation, command, background=background)


async def kick_member_to_standby(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str,
    user_id: str | None = None,
    reason: str = "console_kick",
    unbind_sub2api: bool = False,
    background: bool = False,
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
    operation, blocker = await operation_store.create_workspace_locked(
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
    if blocker is not None:
        return {
            "ok": False,
            "error": f"Workspace {workspace.id} 已有 {blocker.op_type} 任务 {blocker.public_id} 在跑，避免两边同时踢拉",
            "error_code": "operation_conflict",
            "operation_id": blocker.public_id,
        }
    await db.commit()
    target_id, job_id = workspace.id, operation.public_id

    async def command(session: AsyncSession) -> dict[str, Any]:
        return await rotate_service.kick_to_standby(
            session,
            workspace_id=target_id,
            email=target,
            user_id=user_id,
            reason=reason,
            unbind_sub2api=unbind_sub2api,
            job_id=job_id,
        )

    return await _run_command(db, operation, command, background=background)


async def purge_workspace_child(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str,
    user_id: str | None = None,
    reason: str = "console_purge",
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
    if child is not None and (child.local_purpose == "mother" or workspace.owner_account_id == child.id):
        return {"ok": False, "error": "workspace owner cannot be permanently deleted", "error_code": "not_linkable"}
    operation, blocker = await operation_store.create_workspace_locked(
        db,
        op_type="purge_child",
        workspace_id=workspace.id,
        account_id=child.id if child else None,
        email=target,
        input_payload={
            "workspace_id": workspace.id,
            "email": target,
            "user_id": user_id,
            "reason": reason,
            "mode": "purge",
            "unbind_sub2api": True,
            "purge_local": True,
        },
    )
    if blocker is not None:
        return {
            "ok": False,
            "error": f"Workspace {workspace.id} 已有 {blocker.op_type} 任务 {blocker.public_id} 在跑，避免两边同时踢拉",
            "error_code": "operation_conflict",
            "operation_id": blocker.public_id,
        }
    await db.commit()
    result = await rotate_service.kick_to_standby(
        db,
        workspace_id=workspace.id,
        email=target,
        user_id=user_id,
        reason=reason,
        unbind_sub2api=True,
        purge_local=True,
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
    operation, blocker = await operation_store.create_workspace_locked(
        db,
        op_type="revoke_invite",
        workspace_id=workspace.id,
        email=target,
        input_payload={"workspace_id": workspace.id, "email": target, "mode": "revoke_invite"},
    )
    if blocker is not None:
        return {
            "ok": False,
            "error": f"Workspace {workspace.id} 已有 {blocker.op_type} 任务 {blocker.public_id} 在跑，避免两边同时踢拉",
            "error_code": "operation_conflict",
            "operation_id": blocker.public_id,
        }
    await db.commit()
    result = await rotate_service.kick_to_standby(
        db, workspace_id=workspace.id, email=target, invitation_only=True,
        reason="console_revoke", job_id=operation.public_id,
    )
    await operation_store.finish(db, operation, result)
    await db.commit()
    return _ok_result(result, operation_id=operation.public_id)


async def update_account_proxy(
    db: AsyncSession,
    account_id: int,
    *,
    proxy: str | None = None,
    proxy_selection: dict[str, Any] | None = None,
    proxy_profile_id: int | None = None,
    clear: bool = False,
) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    if account.local_purpose != "mother":
        return {"ok": False, "error": "only mother accounts can change proxy here", "error_code": "not_mother"}

    if sum((bool(str(proxy or "").strip()), proxy_selection is not None, proxy_profile_id is not None, clear)) != 1:
        return {"ok": False, "error": "choose exactly one proxy source", "error_code": "proxy_choice_conflict"}

    if clear:
        account.proxy = None
        account.proxy_profile_id = None
        account.proxy_source = None
        account.sub2api_proxy_id = None
        account.proxy_instance_key = None
    elif proxy_selection is not None:
        if str(proxy_selection.get("source") or "") != "sub2api":
            return {"ok": False, "error": "unsupported proxy source", "error_code": "invalid_proxy_source"}
        try:
            resolved = await resolve_sub2api_proxy(db, int(proxy_selection.get("remote_id") or 0))
        except ProxyResolutionError as exc:
            return {"ok": False, "error": str(exc), "error_code": exc.error_code}
        except Exception:
            return {"ok": False, "error": "Sub2API proxy catalog unavailable", "error_code": "remote_catalog_unavailable"}
        profile = await proxy_profile_service.upsert_from_url(
            db,
            resolved.url,
            name=f"Sub2API #{resolved.remote_id}",
            bound_account=account,
        )
        account.proxy = resolved.url
        account.proxy_profile_id = profile.id
        account.proxy_source = resolved.source
        account.sub2api_proxy_id = resolved.remote_id
        account.proxy_instance_key = resolved.instance_key
    elif proxy_profile_id is not None:
        profile = await db.get(ProxyProfile, int(proxy_profile_id))
        if profile is None:
            return {"ok": False, "error": "proxy profile not found", "error_code": "proxy_not_found"}
        account.proxy = proxy_profile_service.compose(profile)
        account.proxy_profile_id = profile.id
        account.proxy_source = "legacy"
        account.sub2api_proxy_id = None
        account.proxy_instance_key = None
    else:
        raw = str(proxy or "").strip()
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
        account.proxy_source = "legacy"
        account.sub2api_proxy_id = None
        account.proxy_instance_key = None

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
        "proxy_source": account.proxy_source,
        "sub2api_proxy_id": account.sub2api_proxy_id,
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
    update_workspace_member_role,
    delete_local_workspace,
)


async def invite_workspace_child(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str,
    role: str = "owner",
    seat_intent: str = "workspace_default",
) -> dict[str, Any]:
    seat_intent = parse_invite_seat_intent(seat_intent).value
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    target = normalize_email(email)
    if not target:
        return {"ok": False, "error": "email required", "error_code": "email_required"}
    operation, blocker = await operation_store.create_workspace_locked(
        db,
        op_type="invite_child",
        workspace_id=workspace.id,
        email=target,
        input_payload={"workspace_id": workspace.id, "email": target, "mode": "invite", "requested_role": parse_invite_role(role), "seat_intent": seat_intent},
    )
    if blocker is not None:
        return {
            "ok": False,
            "error": f"Workspace {workspace.id} 已有 {blocker.op_type} 任务 {blocker.public_id} 在跑，避免两边同时踢拉",
            "error_code": "operation_conflict",
            "operation_id": blocker.public_id,
        }
    await db.commit()
    result = await add_local_child(db, workspace.id, email=target, job_id=operation.public_id, role=role, seat_intent=seat_intent)
    result = {**result, "success": bool(result.get("ok"))}
    await operation_store.finish(db, operation, result)
    await db.commit()
    return _ok_result(result, operation_id=operation.public_id)
