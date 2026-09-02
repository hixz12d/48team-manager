"""Sub2API publish eligibility and verified push/reconcile flows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import ensure_binding, verify_bindings
from app.application.operations import operation_store
from app.application.presenters import present_action_result
from app.application.tokens import decrypt_secret
from app.core.time import utcnow
from app.domain.identity import BINDING_PENDING, BINDING_VERIFIED, PROVIDER_SUB2API
from app.domain.identity.binding import (
    cross_check_binding,
    expected_workspace_id,
    remote_email_from,
    remote_id_from,
    remote_official_account_id_from,
    remote_workspace_id_from,
)
from app.domain.identity.ids import normalize_email
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from app.persistence.repositories import identity as identity_repo


def sub2api_publish_eligibility(account: Account | None) -> dict[str, Any]:
    if account is None:
        return {
            "eligible": False,
            "reason": "账号不存在",
            "missing_fields": ["account"],
            "action": "hidden",
        }
    missing: list[str] = []
    if not account.access_token_encrypted and not account.refresh_token_encrypted:
        missing.append("token")
    if str(account.operational_state or "") in {"archived", "disabled"}:
        missing.append("operational_state")
    if str(account.local_purpose or "") == "disabled":
        missing.append("local_purpose")
    if missing:
        return {
            "eligible": False,
            "reason": "缺少推送所需字段：" + ", ".join(missing),
            "missing_fields": missing,
            "action": "hidden",
        }
    return {
        "eligible": True,
        "reason": "",
        "missing_fields": [],
        "action": "push",
    }


def _binding_for_account(bindings: list[ExternalBinding], account_id: int) -> ExternalBinding | None:
    for row in bindings:
        if int(row.local_account_id) == int(account_id):
            return row
    return None


async def _expected_workspace(db: AsyncSession, account: Account) -> str | None:
    memberships = list(
        (await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.account_id == account.id))).scalars()
    )
    workspaces = await identity_repo.list_workspaces(db)
    workspaces_by_id = {row.id: row for row in workspaces}
    return expected_workspace_id(account, memberships=memberships, workspaces_by_id=workspaces_by_id)


def _build_credentials(account: Account) -> dict[str, Any]:
    access = decrypt_secret(account.access_token_encrypted)
    refresh = decrypt_secret(account.refresh_token_encrypted)
    id_token = decrypt_secret(account.id_token_encrypted)
    creds: dict[str, Any] = {
        "email": normalize_email(account.email),
    }
    if access:
        creds["access_token"] = access
    if refresh:
        creds["refresh_token"] = refresh
    if id_token:
        creds["id_token"] = id_token
    if account.client_id:
        creds["client_id"] = account.client_id
    if account.official_account_id:
        creds["chatgpt_account_id"] = account.official_account_id
    if account.official_user_id:
        creds["chatgpt_user_id"] = account.official_user_id
    return creds


async def account_sub2api_reconcile(db: AsyncSession, account_id: int) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    operation = await operation_store.create(
        db,
        op_type="sub2api_reconcile",
        account_id=account.id,
        email=account.email,
        input_payload={"account_id": account.id, "mode": "reconcile"},
    )
    try:
        remotes = await sub2api_client.list_status_accounts(db)
    except Exception as exc:
        payload = {
            "success": False,
            "ok": False,
            "status": "failed",
            "outcome": "remote_unreachable",
            "message": f"对账失败：无法连接 Sub2API（{exc}）",
            "error": str(exc),
            "error_code": "remote_unreachable",
            "account_id": account.id,
            "email": account.email,
            "binding_state": "unbound",
        }
        await operation_store.mark_step(db, operation, "sub2api_reconcile", state="failed", result=payload, error_message=str(exc))
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return present_action_result(payload, default_error="Sub2API 对账失败")

    report = await verify_bindings(db, remotes)
    bindings = report.get("bindings") or []
    own = next((item for item in bindings if item.get("local_account_id") == account.id), None)
    remote = None
    email_n = normalize_email(account.email)
    for item in remotes or []:
        if remote_email_from(item) == email_n:
            remote = item
            break
        if account.official_account_id and remote_official_account_id_from(item) == str(account.official_account_id):
            remote = item
            break

    if own and own.get("binding_state") == BINDING_VERIFIED:
        outcome = "verified"
        status = "success"
        ok = True
        message = f"对账完成：已验证绑定远端账号 #{own.get('remote_account_id') or own.get('remote_id')}"
        binding_state = BINDING_VERIFIED
    elif own and own.get("binding_state") == "conflict":
        outcome = "conflict"
        status = "manual_required"
        ok = False
        message = f"对账完成：发现冲突（{own.get('last_error') or 'binding conflict'}）"
        binding_state = "conflict"
    elif remote is None:
        outcome = "remote_missing"
        status = "success"
        ok = True
        message = f"对账完成：Sub2API 中未找到 {account.email}，尚未推送。"
        binding_state = "unbound"
    else:
        # Remote exists but not verified binding.
        outcome = "pending"
        status = "success"
        ok = True
        message = f"对账完成：找到远端账号 #{remote_id_from(remote)}，待验证。"
        binding_state = (own or {}).get("binding_state") or "pending"

    payload = {
        "success": ok and status == "success" and outcome != "conflict",
        "ok": ok and outcome != "conflict",
        "status": status,
        "outcome": outcome,
        "message": message,
        "account_id": account.id,
        "email": account.email,
        "binding": own,
        "binding_state": binding_state,
        "remote_count": len(remotes or []),
        "matched": own is not None,
        "remote_id": remote_id_from(remote) if remote else ((own or {}).get("remote_account_id") or (own or {}).get("remote_id")),
    }
    # HTTP succeeded even for remote_missing; keep operation success but yellow UI via outcome.
    if outcome == "conflict":
        payload["success"] = False
        payload["ok"] = False
        payload["partial"] = False
    await operation_store.mark_step(
        db,
        operation,
        "sub2api_reconcile",
        state="success" if payload.get("success") or outcome == "remote_missing" else ("manual_required" if outcome == "conflict" else "failed"),
        result=payload,
        error_message="" if payload.get("success") or outcome in {"remote_missing", "pending"} else message,
    )
    finish_payload = dict(payload)
    if outcome == "remote_missing":
        finish_payload["success"] = True
        finish_payload["status"] = "success"
    elif outcome == "conflict":
        finish_payload["success"] = False
        finish_payload["status"] = "manual_required"
    await operation_store.finish(db, operation, finish_payload)
    await db.commit()
    return present_action_result({"ok": payload["ok"], "operation_id": operation.public_id, **payload})


async def account_sub2api_push(
    db: AsyncSession,
    account_id: int,
    *,
    group_ids: list[int] | None = None,
    name: str | None = None,
    schedulable: bool | None = True,
    confirm_mixed_channel_risk: bool = False,
) -> dict[str, Any]:
    account = await db.get(Account, int(account_id))
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    eligibility = sub2api_publish_eligibility(account)
    if not eligibility.get("eligible"):
        return {
            "ok": False,
            "error": eligibility.get("reason") or "not eligible",
            "error_code": "not_eligible",
            "outcome": "not_eligible",
            "status": "failed",
            "message": eligibility.get("reason") or "该账号不适用 Sub2API 推送",
            **eligibility,
        }

    operation = await operation_store.create(
        db,
        op_type="sub2api_push",
        account_id=account.id,
        email=account.email,
        input_payload={
            "account_id": account.id,
            "mode": "push",
            "group_ids": list(group_ids or []),
            "name": name,
            "schedulable": schedulable,
        },
    )

    existing = (
        await db.execute(
            select(ExternalBinding).where(
                ExternalBinding.provider == PROVIDER_SUB2API,
                ExternalBinding.local_account_id == account.id,
            )
        )
    ).scalar_one_or_none()

    credentials = _build_credentials(account)
    if not credentials.get("access_token") and not credentials.get("refresh_token"):
        payload = {
            "success": False,
            "ok": False,
            "status": "failed",
            "outcome": "not_eligible",
            "error_code": "missing_token",
            "error": "缺少 Access/Refresh Token",
            "message": "推送失败：缺少 Access/Refresh Token",
            "account_id": account.id,
        }
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return present_action_result({"operation_id": operation.public_id, **payload})

    expected_ws = await _expected_workspace(db, account)
    if expected_ws and "organization_id" not in credentials:
        credentials["organization_id"] = expected_ws
        credentials["workspace_id"] = expected_ws

    account_name = str(name or "").strip() or normalize_email(account.email)
    body: dict[str, Any] = {
        "name": account_name,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {
            "email": normalize_email(account.email),
            "chatgpt_account_id": account.official_account_id,
            "chatgpt_user_id": account.official_user_id,
            "workspace_id": expected_ws,
            "source": "team48-manager",
        },
        "concurrency": 1,
        "priority": 1,
    }
    if group_ids:
        body["group_ids"] = [int(x) for x in group_ids if int(x) > 0]
    if confirm_mixed_channel_risk:
        body["confirm_mixed_channel_risk"] = True

    try:
        if existing and existing.remote_account_id:
            remote_id = int(existing.remote_account_id)
            written = await sub2api_client.update_account(db, remote_id, body)
            action = "update"
        else:
            # Try match by email first to avoid duplicates.
            matched = await sub2api_client.find_account_by_email(db, account.email)
            if matched and remote_id_from(matched):
                remote_id = int(remote_id_from(matched))
                written = await sub2api_client.update_account(db, remote_id, body)
                action = "update"
            else:
                written = await sub2api_client.create_account(db, body)
                action = "create"
                remote_id = int(remote_id_from(written) or 0)
        await operation_store.mark_step(
            db,
            operation,
            "sub2api_push",
            state="success",
            result={"action": action, "remote_id": remote_id, "written_keys": sorted(list(written.keys()))[:12]},
        )
    except Exception as exc:
        payload = {
            "success": False,
            "ok": False,
            "status": "failed",
            "outcome": "push_failed",
            "error": str(exc),
            "error_code": "push_failed",
            "message": f"推送失败：{exc}",
            "account_id": account.id,
        }
        await operation_store.mark_step(db, operation, "sub2api_push", state="failed", result=payload, error_message=str(exc))
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return present_action_result({"operation_id": operation.public_id, **payload})

    if not remote_id:
        remote_id = int(remote_id_from(written) or 0)
    if not remote_id:
        payload = {
            "success": False,
            "ok": False,
            "status": "partial",
            "partial": True,
            "outcome": "verification_failed",
            "error_code": "missing_remote_id",
            "error": "写入响应缺少远端 ID",
            "message": "写入请求可能已成功，但响应没有远端 ID，未建立 Binding",
            "account_id": account.id,
        }
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return present_action_result({"operation_id": operation.public_id, **payload})

    # Read-after-write verification.
    try:
        remote = await sub2api_client.read_after_write(db, remote_id)
    except Exception as exc:
        payload = {
            "success": False,
            "ok": False,
            "status": "partial",
            "partial": True,
            "outcome": "verification_failed",
            "error": str(exc),
            "error_code": "verification_failed",
            "message": f"写入请求已成功，但复读失败，未建立 verified Binding：{exc}",
            "account_id": account.id,
            "remote_id": remote_id,
        }
        await operation_store.mark_step(db, operation, "read_after_write", state="failed", result=payload, error_message=str(exc))
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return present_action_result({"operation_id": operation.public_id, **payload})

    check_state, check_error = cross_check_binding(
        local_email=account.email,
        local_official_account_id=account.official_account_id,
        expected_workspace=expected_ws,
        remote=remote,
    )
    if check_state != BINDING_VERIFIED:
        # Still record pending binding for triage, never verified.
        await ensure_binding(db, account=account, remote_account_id=remote_id)
        binding = (
            await db.execute(
                select(ExternalBinding).where(
                    ExternalBinding.provider == PROVIDER_SUB2API,
                    ExternalBinding.local_account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        if binding is not None:
            binding.binding_state = BINDING_PENDING if check_state == BINDING_PENDING else check_state
            binding.last_error = str(check_error or "verification mismatch")
            binding.last_observed_at = utcnow()
        payload = {
            "success": False,
            "ok": False,
            "status": "manual_required",
            "partial": True,
            "outcome": "verification_failed",
            "error_code": "verification_failed",
            "error": check_error or "read-after-write mismatch",
            "message": "写入请求已成功，但复读验证不一致，未建立 verified Binding",
            "account_id": account.id,
            "remote_id": remote_id,
            "check": {"state": check_state, "error": check_error},
            "remote_email": remote_email_from(remote),
            "remote_official_account_id": remote_official_account_id_from(remote),
            "remote_workspace_id": remote_workspace_id_from(remote),
        }
        await operation_store.mark_step(db, operation, "read_after_write", state="failed", result=payload, error_message=payload["error"])
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return present_action_result({"operation_id": operation.public_id, **payload})

    await ensure_binding(db, account=account, remote_account_id=remote_id)
    binding = (
        await db.execute(
            select(ExternalBinding).where(
                ExternalBinding.provider == PROVIDER_SUB2API,
                ExternalBinding.local_account_id == account.id,
            )
        )
    ).scalar_one_or_none()
    if binding is not None:
        binding.binding_state = BINDING_VERIFIED
        binding.verified_email = remote_email_from(remote) or normalize_email(account.email)
        binding.verified_official_account_id = remote_official_account_id_from(remote) or account.official_account_id
        binding.verified_workspace_id = remote_workspace_id_from(remote) or expected_ws
        binding.last_error = None
        binding.last_observed_at = utcnow()
        binding.updated_at = utcnow()

    if schedulable is not None:
        try:
            await sub2api_client.set_account_schedulable(db, remote_id, bool(schedulable))
        except Exception:
            pass

    group_label = ",".join(str(x) for x in (group_ids or [])) or "默认分组"
    payload = {
        "success": True,
        "ok": True,
        "status": "success",
        "outcome": "verified",
        "message": f"推送成功：远端账号 #{remote_id}，分组 {group_label}，复读验证通过",
        "account_id": account.id,
        "email": account.email,
        "remote_id": remote_id,
        "action": action,
        "binding_state": BINDING_VERIFIED,
        "group_ids": list(group_ids or []),
    }
    await operation_store.mark_step(db, operation, "read_after_write", state="success", result=payload)
    await operation_store.finish(db, operation, payload)
    await db.commit()
    return present_action_result({"operation_id": operation.public_id, **payload})


# Backward-compatible alias used by typed retry of old operations.
async def account_sub2api_sync(db: AsyncSession, account_id: int) -> dict[str, Any]:
    return await account_sub2api_reconcile(db, account_id)
