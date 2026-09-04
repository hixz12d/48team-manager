"""Sub2API publish eligibility and verified push/reconcile flows."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import ensure_binding, verify_bindings
from app.application.operations import operation_store
from app.application.presenters import present_action_result
from app.application.sub2api_proxy import sub2api_proxy_service
from app.application.tokens import decrypt_secret
from app.core.time import utcnow
from app.domain.identity import BINDING_PENDING, BINDING_VERIFIED, PROVIDER_SUB2API
from app.domain.identity.binding import (
    AmbiguousWorkspaceContext,
    canonical_name_for_account,
    cross_check_binding,
    expected_workspace_id,
    remote_context_key_from,
    remote_email_from,
    remote_id_from,
    remote_official_account_id_from,
    remote_workspace_id_from,
    resolve_workspace_context,
    team48_context_key,
)
from app.domain.identity.ids import normalize_email
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from app.persistence.models.sub2api import Sub2ApiProxyBinding
from app.persistence.repositories import identity as identity_repo


def _is_remote_missing(exc: Exception) -> bool:
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 404:
        return True
    text = str(exc)
    return "404 Not Found" in text and "/admin/accounts/" in text


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


def _binding_for_account(
    bindings: list[ExternalBinding],
    account_id: int,
    workspace_id: int | None = None,
) -> ExternalBinding | None:
    scoped = None
    unscoped = None
    for row in bindings:
        if int(row.local_account_id) != int(account_id):
            continue
        if workspace_id is not None and row.workspace_id == int(workspace_id):
            return row
        if row.workspace_id is None:
            unscoped = row
        elif scoped is None:
            scoped = row
    return scoped or unscoped


async def _resolve_push_context(
    db: AsyncSession,
    account: Account,
    workspace_id: int | None = None,
) -> tuple[Workspace | None, str | None]:
    memberships = list(
        (await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.account_id == account.id))).scalars()
    )
    workspaces_by_id = {row.id: row for row in await identity_repo.list_workspaces(db)}
    workspace = resolve_workspace_context(
        account,
        memberships=memberships,
        workspaces_by_id=workspaces_by_id,
        workspace_id=workspace_id,
    )
    official = expected_workspace_id(
        account,
        memberships=memberships,
        workspaces_by_id=workspaces_by_id,
        workspace_id=workspace.id if workspace is not None else None,
    )
    return workspace, official


def _match_remote(
    remotes: list[dict[str, Any]],
    *,
    context_key: str | None = None,
    existing_remote_id: str | None = None,
) -> dict[str, Any] | None:
    if existing_remote_id:
        for item in remotes:
            if remote_id_from(item) == str(existing_remote_id):
                return item
    key = str(context_key or "").strip()
    if not key:
        return None
    matches = [item for item in remotes if remote_context_key_from(item) == key]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousWorkspaceContext("multiple Sub2API accounts match this team48 context key")
    return None


async def _expected_workspace(db: AsyncSession, account: Account, workspace_id: int | None = None) -> str | None:
    _workspace, official = await _resolve_push_context(db, account, workspace_id)
    return official


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
    schedulable: bool | None = None,
    confirm_mixed_channel_risk: bool = False,
    workspace_id: int | None = None,
    template_id: str | None = None,
    template_overrides: dict[str, Any] | None = None,
    proxy_source: str | None = None,
    proxy_profile_id: int | None = None,
    reapply_template: bool = False,
    test_proxy_before_push: bool = False,
    dry_run: bool = False,
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

    try:
        workspace, expected_ws = await _resolve_push_context(db, account, workspace_id)
    except AmbiguousWorkspaceContext as exc:
        return {
            "ok": False,
            "error": str(exc),
            "error_code": "ambiguous_workspace_context",
            "status": "failed",
            "message": "该账号属于多个 Workspace，请先选择要推送的上下文",
        }

    scoped_workspace_id = workspace.id if workspace is not None else None
    existing = (
        await db.execute(
            select(ExternalBinding).where(
                ExternalBinding.provider == PROVIDER_SUB2API,
                ExternalBinding.local_account_id == account.id,
                ExternalBinding.workspace_id == scoped_workspace_id,
            )
        )
    ).scalar_one_or_none()
    if existing is None and scoped_workspace_id is not None:
        existing = (
            await db.execute(
                select(ExternalBinding).where(
                    ExternalBinding.provider == PROVIDER_SUB2API,
                    ExternalBinding.local_account_id == account.id,
                    ExternalBinding.workspace_id.is_(None),
                )
            )
        ).scalar_one_or_none()

    normalized_groups = None
    if group_ids is not None:
        if any(int(value) <= 0 for value in group_ids):
            return {
                "ok": False,
                "status": "failed",
                "error_code": "invalid_group_ids",
                "error": "group_ids must contain positive integers",
            }
        normalized_groups = [int(value) for value in group_ids]

    if template_overrides and not template_id:
        return {
            "ok": False,
            "status": "failed",
            "error_code": "template_id_required",
            "error": "template_overrides requires template_id",
        }
    if reapply_template and not template_id:
        return {
            "ok": False,
            "status": "failed",
            "error_code": "template_id_required",
            "error": "reapply_template requires template_id",
        }
    if existing is not None and template_id and not reapply_template:
        return {
            "ok": False,
            "status": "failed",
            "error_code": "template_reapply_required",
            "error": "updating an existing account from a template requires reapply_template=true",
        }

    template_requested = bool(template_id or template_overrides or reapply_template)
    capabilities = None
    if template_requested:
        try:
            capabilities = await sub2api_client.integration_capabilities(db)
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "error_code": "template_capability_unavailable",
                "error": str(exc),
                "message": "无法确认 Sub2API 账号模板能力",
            }
        template_caps = capabilities.get("account_templates") or {}
        required = "apply" if reapply_template or existing is not None else "create_from_template"
        if not template_caps.get(required):
            return {
                "ok": False,
                "status": "unsupported",
                "error_code": "account_templates_unsupported",
                "error": "当前 Sub2API 不支持账号推送模板",
                "message": "当前 Sub2API 没有账号模板 CRUD/apply 合约；未发送任何写请求",
                "capabilities": capabilities,
            }

    credentials = _build_credentials(account)
    if not credentials.get("access_token") and not credentials.get("refresh_token"):
        return {
            "success": False,
            "ok": False,
            "status": "failed",
            "outcome": "not_eligible",
            "error_code": "missing_token",
            "error": "缺少 Access/Refresh Token",
            "message": "推送失败：缺少 Access/Refresh Token",
            "account_id": account.id,
        }
    if expected_ws:
        credentials.setdefault("organization_id", expected_ws)
        credentials.setdefault("workspace_id", expected_ws)

    owner_email = None
    if workspace is not None and workspace.owner_account_id:
        owner = await db.get(Account, int(workspace.owner_account_id))
        owner_email = owner.email if owner is not None else None
    canonical_name = canonical_name_for_account(
        account,
        workspace if workspace is not None else type("WS", (), {"owner_account_id": None})(),
        owner_email=owner_email,
    )
    account_name = str(name or "").strip() or canonical_name

    effective_proxy_source = proxy_source or ("preserve" if existing is not None else "account")
    selected_profile_id = int(proxy_profile_id) if proxy_profile_id else None
    if selected_profile_id is None and effective_proxy_source == "account":
        selected_profile_id = account.proxy_profile_id
    proxy_preview = {
        "source": "selected" if proxy_profile_id else effective_proxy_source,
        "local_proxy_profile_id": selected_profile_id,
        "remote_proxy_id": None,
        "explicit": proxy_source is not None or proxy_profile_id is not None,
    }
    if selected_profile_id:
        proxy_mapping = (
            await db.execute(
                select(Sub2ApiProxyBinding).where(
                    Sub2ApiProxyBinding.local_proxy_profile_id == selected_profile_id
                )
            )
        ).scalar_one_or_none()
        if proxy_mapping is not None:
            proxy_preview["remote_proxy_id"] = proxy_mapping.remote_proxy_id
            proxy_preview["sync_state"] = proxy_mapping.sync_state
        else:
            proxy_preview["sync_state"] = "unbound"
    if dry_run:
        create_fields = ["name", "platform", "type", "credentials", "extra", "concurrency", "priority"]
        update_fields = ["credentials"]
        if name:
            update_fields.append("name")
        if normalized_groups is not None:
            update_fields.append("group_ids")
        if selected_profile_id or proxy_source == "none":
            update_fields.append("proxy_id")
        if schedulable is not None:
            update_fields.append("schedulable")
        return {
            "success": True,
            "ok": True,
            "status": "preview",
            "dry_run": True,
            "account_id": account.id,
            "workspace_id": scoped_workspace_id,
            "remote_account_id": existing.remote_account_id if existing else None,
            "action": "update" if existing else "create",
            "effective_name": account_name,
            "template_id": template_id,
            "template_reapplied": bool(reapply_template),
            "group_ids": normalized_groups,
            "proxy": proxy_preview,
            "would_update": update_fields if existing else create_fields,
            "would_preserve": [] if not existing else [
                value for value in (
                    "concurrency",
                    "priority",
                    "rate_multiplier",
                    "load_factor",
                    "extra",
                    "expires_at",
                    "auto_pause_on_expired",
                    "group_ids" if normalized_groups is None else None,
                    "proxy_id" if not selected_profile_id and proxy_source != "none" else None,
                ) if value
            ],
            "warnings": (
                ["远端代理清空需要 Sub2API clear_proxy 合约"]
                if existing and proxy_source == "none"
                else []
            ),
        }

    operation = await operation_store.create(
        db,
        op_type="sub2api_push",
        account_id=account.id,
        email=account.email,
        workspace_id=scoped_workspace_id or 0,
        input_payload={
            "account_id": account.id,
            "workspace_id": scoped_workspace_id,
            "mode": "reapply_template" if reapply_template else "credential_sync",
            "group_ids": normalized_groups,
            "schedulable": schedulable,
            "template_id": template_id,
            "template_override_keys": sorted((template_overrides or {}).keys()),
            "proxy_source": proxy_source,
            "proxy_profile_id": proxy_profile_id,
            "test_proxy_before_push": test_proxy_before_push,
        },
    )

    try:
        if existing is not None and proxy_source is None and proxy_profile_id is None:
            proxy_resolution = {
                "source": "preserve",
                "local_proxy_profile_id": None,
                "remote_proxy_id": None,
                "explicit": False,
            }
        else:
            proxy_resolution = await sub2api_proxy_service.resolve_for_push(
                db,
                account,
                proxy_source=proxy_source or "account",
                proxy_profile_id=proxy_profile_id,
                template_id=template_id,
                test_before_use=test_proxy_before_push,
            )
    except Exception as exc:
        payload = {
            "success": False,
            "ok": False,
            "status": "failed",
            "outcome": "proxy_sync_failed",
            "error_code": "proxy_sync_failed",
            "error": str(exc),
            "message": f"推送前代理同步失败：{exc}",
            "account_id": account.id,
        }
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return present_action_result({"operation_id": operation.public_id, **payload})

    if existing and proxy_source == "none":
        try:
            remote_before = await sub2api_client.get_account(db, int(existing.remote_account_id))
        except Exception as exc:
            remote_before = {} if _is_remote_missing(exc) else None
            if remote_before is None:
                payload = {
                    "success": False,
                    "ok": False,
                    "status": "failed",
                    "error_code": "remote_read_failed",
                    "error": str(exc),
                    "message": "无法确认远端代理状态，未执行代理清空",
                }
                await operation_store.finish(db, operation, payload)
                await db.commit()
                return present_action_result({"operation_id": operation.public_id, **payload})
        if remote_before.get("proxy_id") not in (None, "", 0, "0"):
            payload = {
                "success": False,
                "ok": False,
                "status": "unsupported",
                "error_code": "remote_proxy_clear_unsupported",
                "error": "Sub2API update API cannot distinguish omitted proxy_id from null",
                "message": "当前 Sub2API 不支持安全清空已有账号代理；未修改远端账号",
            }
            await operation_store.finish(db, operation, payload)
            await db.commit()
            return present_action_result({"operation_id": operation.public_id, **payload})

    create_body: dict[str, Any] = {
        "name": account_name,
        "platform": "openai",
        "type": "oauth",
        "credentials": credentials,
        "extra": {
            "email": normalize_email(account.email),
            "chatgpt_account_id": account.official_account_id,
            "chatgpt_user_id": account.official_user_id,
            "workspace_id": expected_ws,
            "organization_id": expected_ws,
            "source": "team48-manager",
            "team48_context_key": team48_context_key(expected_ws, account.id),
        },
        "concurrency": 1,
        "priority": 1,
    }
    update_body: dict[str, Any] = {"credentials": credentials}
    if name:
        update_body["name"] = account_name
    if normalized_groups is not None:
        create_body["group_ids"] = normalized_groups
        update_body["group_ids"] = normalized_groups
    if proxy_resolution.get("remote_proxy_id"):
        create_body["proxy_id"] = int(proxy_resolution["remote_proxy_id"])
        update_body["proxy_id"] = int(proxy_resolution["remote_proxy_id"])
    if template_id:
        create_body["template_id"] = template_id
        if template_overrides is not None:
            create_body["template_overrides"] = template_overrides
        if reapply_template:
            update_body["template_id"] = template_id
            update_body["reapply_template"] = True
            if template_overrides is not None:
                update_body["template_overrides"] = template_overrides
    if confirm_mixed_channel_risk:
        create_body["confirm_mixed_channel_risk"] = True
        update_body["confirm_mixed_channel_risk"] = True

    try:
        written = None
        remote_id = 0
        action = "create"
        stale_binding = False
        if existing and existing.remote_account_id:
            remote_id = int(existing.remote_account_id)
            try:
                written = await sub2api_client.update_account(db, remote_id, update_body)
                action = "update"
            except Exception as exc:
                if not _is_remote_missing(exc):
                    raise
                await db.delete(existing)
                await db.flush()
                existing = None
                stale_binding = True
                remote_id = 0
        if written is None:
            matched = None
            try:
                matched = _match_remote(
                    await sub2api_client.list_status_accounts(db),
                    context_key=team48_context_key(expected_ws, account.id),
                )
            except Exception:
                matched = None
            if matched and remote_id_from(matched):
                remote_id = int(remote_id_from(matched))
                try:
                    written = await sub2api_client.update_account(db, remote_id, update_body)
                    action = "update"
                except Exception as inner:
                    if not _is_remote_missing(inner):
                        raise
                    remote_id = 0
                    written = None
            if written is None:
                written = await sub2api_client.create_account(db, create_body)
                action = "recreate" if stale_binding else "create"
                remote_id = int(remote_id_from(written) or 0)
        written_keys = sorted(list(written.keys()))[:12] if isinstance(written, dict) else []
        await operation_store.mark_step(
            db,
            operation,
            "sub2api_push",
            state="success",
            result={"action": action, "remote_id": remote_id, "written_keys": written_keys},
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
        await operation_store.mark_step(
            db, operation, "sub2api_push", state="failed", result=payload, error_message=str(exc)
        )
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
        await operation_store.mark_step(
            db, operation, "read_after_write", state="failed", result=payload, error_message=str(exc)
        )
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
        await ensure_binding(db, account=account, remote_account_id=remote_id, workspace_id=scoped_workspace_id)
        binding = (
            await db.execute(
                select(ExternalBinding).where(
                    ExternalBinding.provider == PROVIDER_SUB2API,
                    ExternalBinding.local_account_id == account.id,
                    ExternalBinding.workspace_id == scoped_workspace_id,
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
        await operation_store.mark_step(
            db, operation, "read_after_write", state="failed", result=payload, error_message=payload["error"]
        )
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return present_action_result({"operation_id": operation.public_id, **payload})

    await ensure_binding(db, account=account, remote_account_id=remote_id, workspace_id=scoped_workspace_id)
    binding = (
        await db.execute(
            select(ExternalBinding).where(
                ExternalBinding.provider == PROVIDER_SUB2API,
                ExternalBinding.local_account_id == account.id,
                ExternalBinding.workspace_id == scoped_workspace_id,
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

    schedulable_error = None
    if schedulable is not None:
        try:
            await sub2api_client.set_account_schedulable(db, remote_id, bool(schedulable))
        except Exception as exc:
            schedulable_error = str(exc)

    updated_fields = sorted(update_body.keys()) if action == "update" else sorted(create_body.keys())
    if schedulable is not None:
        updated_fields.append("schedulable")
        updated_fields.sort()
    preserved_fields = [] if action != "update" else [
        "concurrency", "priority", "rate_multiplier", "load_factor", "extra", "expires_at", "auto_pause_on_expired"
    ]
    if normalized_groups is None and action == "update":
        preserved_fields.append("group_ids")
    if not proxy_resolution.get("remote_proxy_id") and proxy_source != "none" and action == "update":
        preserved_fields.append("proxy_id")

    base_result = {
        "account_id": account.id,
        "email": account.email,
        "remote_id": remote_id,
        "remote_account_id": str(remote_id),
        "action": action,
        "binding_state": BINDING_VERIFIED,
        "canonical_name": canonical_name,
        "effective_name": account_name,
        "workspace_id": scoped_workspace_id,
        "group_ids": normalized_groups,
        "template_id": template_id,
        "template_reapplied": bool(reapply_template),
        "local_proxy_profile_id": proxy_resolution.get("local_proxy_profile_id"),
        "remote_proxy_id": proxy_resolution.get("remote_proxy_id"),
        "proxy_source": proxy_resolution.get("source"),
        "updated_fields": updated_fields,
        "preserved_fields": preserved_fields,
        "warnings": [],
    }
    if schedulable_error:
        payload = {
            **base_result,
            "success": False,
            "ok": False,
            "status": "partial",
            "partial": True,
            "outcome": "verification_failed",
            "error_code": "schedulable_failed",
            "error": schedulable_error,
            "message": f"推送已写入远端账号 #{remote_id}，但 schedulable 更新失败：{schedulable_error}",
        }
        await operation_store.mark_step(
            db, operation, "read_after_write", state="partial", result=payload, error_message=schedulable_error
        )
        await operation_store.finish(db, operation, payload)
        await db.commit()
        return present_action_result({"operation_id": operation.public_id, **payload})

    group_label = "继承/保持" if normalized_groups is None else (",".join(str(x) for x in normalized_groups) or "已清空")
    payload = {
        **base_result,
        "success": True,
        "ok": True,
        "status": "success",
        "outcome": "verified",
        "message": f"推送成功：远端账号 #{remote_id}，分组 {group_label}，复读验证通过",
    }
    await operation_store.mark_step(db, operation, "read_after_write", state="success", result=payload)
    await operation_store.finish(db, operation, payload)
    await db.commit()
    return present_action_result({"operation_id": operation.public_id, **payload})


# Backward-compatible alias used by typed retry of old operations.
async def account_sub2api_sync(db: AsyncSession, account_id: int) -> dict[str, Any]:
    return await account_sub2api_reconcile(db, account_id)
