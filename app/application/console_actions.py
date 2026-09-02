"""Console action services: operations, HME, accounts, proxy repair."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import verify_bindings
from app.application.operations import operation_store, serialize_operation
from app.application.quota import quota_service
from app.application.reauth import reauth_service
from app.application.resources.hme import (
    ClaimedAlias,
    apply_local_label,
    reconcile_aliases,
    release_lease,
)
from app.application.resources.proxies import proxy_profile_service
from app.application.tokens import auth_service
from app.core.proxy import mask_proxy_url
from app.core.time import isoformat, utcnow
from app.domain.resources import (
    HME_STATE_RESERVED,
)
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account
from app.persistence.models.operations import OperationStep
from app.persistence.models.resources import HmeAliasLease

SAFE_RETRY_TYPES = {"quota_probe", "auth_probe", "proxy_check", "workspace_sync", "reconcile", "sub2api_sync"}
UNSAFE_RETRY_TYPES = {"onboard", "rotate", "reauth", "free_register", "reregister", "free"}


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
    clone = await operation_store.create(
        db,
        op_type=row.op_type,
        workspace_id=int(row.workspace_id or 0),
        account_id=row.account_id,
        email=row.email or "",
        phone=row.phone or "",
        input_payload={"retry_of": row.public_id},
        resolved_proxy=row.resolved_proxy or "",
        resolved_proxy_profile_id=row.resolved_proxy_profile_id,
    )
    await operation_store.note(db, clone, "queued", f"retry of {row.public_id}")
    await operation_store.finish(
        db,
        clone,
        {
            "success": True,
            "status": "success",
            "message": "retry ticket accepted; runner must pick safe typed handlers",
            "retry_of": row.public_id,
        },
    )
    await db.commit()
    return {"ok": True, "operation_id": clone.public_id, "retry_of": row.public_id}


async def run_hme_reconcile(db: AsyncSession) -> dict[str, Any]:
    operation = await operation_store.create(db, op_type="reconcile", input_payload={"mode": "hme_readonly"})
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
        op_type="reconcile",
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
                name=f"母号 {account.email}" if account.email else "",
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


async def account_quota_probe(db: AsyncSession, account_id: int) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    operation = await operation_store.create(
        db,
        op_type="quota_probe",
        account_id=account.id,
        email=account.email,
        input_payload={"account_id": account.id},
    )
    snap = await quota_service.probe_account(db, account)
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
    result = await reauth_service.start_auto_reauth(db, account)
    operation_id = result.get("operation_id") or result.get("public_id") or result.get("job_id")
    return {"ok": bool(result.get("success")), "operation_id": operation_id, **result}


async def account_sub2api_sync(db: AsyncSession, account_id: int) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    operation = await operation_store.create(
        db,
        op_type="sub2api_sync",
        account_id=account.id,
        email=account.email,
        input_payload={"account_id": account.id},
    )
    remotes = await sub2api_client.list_status_accounts(db)
    report = await verify_bindings(db, remotes)
    payload = {
        "success": True,
        "status": "success",
        "account_id": account.id,
        "remote_count": len(remotes or []),
        "report": report,
    }
    await operation_store.mark_step(db, operation, "sub2api_sync", state="success", result=payload)
    await operation_store.finish(db, operation, payload)
    await db.commit()
    return {"ok": True, "operation_id": operation.public_id, **payload}


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

    ok = any(item.get("ok") for item in steps.values())
    payload = {
        "success": ok,
        "status": "success" if ok else "failed",
        "partial": not all(item.get("ok") for item in steps.values()),
        "steps": steps,
        "account_id": account.id,
    }
    await operation_store.finish(db, operation, payload)
    await db.commit()
    return {"ok": ok, "operation_id": operation.public_id, **payload}
