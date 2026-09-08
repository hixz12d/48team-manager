"""Shared Sub2API credential sync + recovery assessment for Team48 push paths."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.identity.ids import normalize_email
from app.integrations.sub2api.client import sub2api_client

SyncReason = Literal["manual_reauthorize", "background_refresh", "manual_push"]


def _identity_from_remote(remote: dict[str, Any] | None) -> dict[str, Any]:
    payload = remote if isinstance(remote, dict) else {}
    credentials = payload.get("credentials") if isinstance(payload.get("credentials"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    return {
        "email": normalize_email(
            credentials.get("email") or extra.get("email") or payload.get("email") or ""
        ),
        "workspace_id": str(
            credentials.get("workspace_id")
            or credentials.get("organization_uuid")
            or credentials.get("organization_id")
            or extra.get("workspace_id")
            or payload.get("workspace_id")
            or ""
        )
        or None,
        "status": str(payload.get("status") or ""),
        "error_message": str(payload.get("error_message") or payload.get("error") or ""),
        "schedulable": payload.get("schedulable"),
        "updated_at": payload.get("updated_at") or payload.get("updatedAt"),
    }


def assess_auth_error(remote: dict[str, Any] | None) -> bool:
    """Heuristic for recognizable auth-only error state. Conservative by design."""
    info = _identity_from_remote(remote)
    status = str(info.get("status") or "").strip().lower()
    message = str(info.get("error_message") or "").strip().lower()
    if status not in {"error", "unauthorized", "auth_error"}:
        return False
    if not message:
        return False
    markers = (
        "401",
        "unauthorized",
        "token_expired",
        "token is expired",
        "invalid_token",
        "authentication",
        "oauth",
        "access token",
        "refresh token",
    )
    return any(marker in message for marker in markers)


def choose_recovery_mode(reason: SyncReason, *, remote: dict[str, Any] | None, auth_validated: bool) -> str:
    if reason == "background_refresh":
        return "credentials_only"
    if reason in {"manual_reauthorize", "manual_push"} and auth_validated and assess_auth_error(remote):
        return "auth_only"
    return "credentials_only"


def present_sync_message(result: dict[str, Any]) -> str:
    remote_id = result.get("remote_account_id") or result.get("remote_id") or "?"
    if not result.get("supported", True) and result.get("error_code") == "sync_oauth_unsupported":
        return f"凭据已按兼容路径同步至原账号 #{remote_id}；远端安全恢复能力不可用，旧鉴权错误未自动清理。"
    write = result.get("credential_write")
    recovery = result.get("auth_recovery")
    cache = result.get("token_cache_invalidation")
    schedulable = result.get("schedulable")
    parts: list[str] = []
    if write == "succeeded":
        parts.append(f"凭据已同步至原账号 #{remote_id}")
    elif write:
        parts.append(f"凭据写入状态：{write}")
    if recovery == "cleared":
        parts.append("旧授权错误已清除")
    elif recovery == "skipped":
        parts.append("未执行鉴权错误清理")
    elif recovery == "not_applicable":
        parts.append("当前无确认的鉴权错误可清")
    elif recovery == "conflict":
        parts.append("鉴权恢复发生冲突，未覆盖新状态")
    elif recovery and recovery not in {"unknown"}:
        parts.append(f"鉴权恢复：{recovery}")
    if cache == "failed":
        parts.append("旧 Token 缓存失效失败，恢复未完成")
    elif cache == "succeeded":
        parts.append("旧 Token 缓存已失效")
    if schedulable is False:
        parts.append("调度开关仍关闭，未自动更改暂停设置")
    blockers = [str(item) for item in (result.get("remaining_blockers") or []) if item]
    if blockers:
        parts.append("仍有限制：" + "、".join(blockers[:4]))
    if result.get("error") and not result.get("ok"):
        parts.append(str(result.get("error")))
    return "；".join(parts) if parts else "凭据同步已完成"


async def sync_bound_oauth_credentials(
    db: AsyncSession,
    *,
    remote_id: int,
    credentials: dict[str, Any],
    expected_email: str | None = None,
    expected_workspace_id: str | None = None,
    operation_id: str | None = None,
    reason: SyncReason = "manual_push",
    auth_validated: bool = False,
    prefetched_remote: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """GET → narrow sync (or legacy credentials update) → final GET assessment."""
    remote = prefetched_remote
    if remote is None:
        try:
            remote = await sub2api_client.get_account(db, int(remote_id))
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "supported": True,
                "error_code": "remote_get_failed",
                "error": f"写前读取远端账号失败：{exc}",
                "remote_account_id": remote_id,
                "credential_write": "not_attempted",
                "auth_recovery": "skipped",
                "token_cache_invalidation": "skipped",
            }

    before = _identity_from_remote(remote)
    if expected_email:
        remote_email = before.get("email") or ""
        if remote_email and normalize_email(expected_email) != remote_email:
            return {
                "ok": False,
                "supported": True,
                "error_code": "identity_mismatch",
                "error": "远端邮箱与本地账号不一致，未写入",
                "remote_account_id": remote_id,
                "credential_write": "not_attempted",
                "auth_recovery": "skipped",
                "token_cache_invalidation": "skipped",
                "expected_email": normalize_email(expected_email),
                "remote_email": remote_email,
            }

    recovery_mode = choose_recovery_mode(reason, remote=remote, auth_validated=bool(auth_validated))
    expected_identity = {
        "email": normalize_email(expected_email or "") or None,
        "workspace_id": expected_workspace_id or None,
    }
    narrow = await sub2api_client.sync_oauth_credentials(
        db,
        int(remote_id),
        credentials=credentials,
        expected_identity=expected_identity,
        expected_updated_at=str(before.get("updated_at") or "") or None,
        operation_id=operation_id,
        recovery_mode=recovery_mode,
    )

    if narrow.get("supported") is False:
        try:
            await sub2api_client.update_account(db, int(remote_id), {"credentials": credentials})
            after = await sub2api_client.read_after_write(db, int(remote_id))
        except Exception as exc:  # noqa: BLE001
            result = {
                "ok": False,
                "supported": False,
                "error_code": "legacy_credential_update_failed",
                "error": str(exc),
                "remote_account_id": remote_id,
                "credential_write": "failed",
                "auth_recovery": "skipped",
                "token_cache_invalidation": "unknown",
            }
            result["message"] = present_sync_message(
                {
                    **result,
                    "error_code": "sync_oauth_unsupported",
                }
            )
            return result
        after_info = _identity_from_remote(after)
        result = {
            "ok": True,
            "supported": False,
            "error_code": "sync_oauth_unsupported",
            "remote_account_id": remote_id,
            "credential_write": "succeeded",
            "auth_recovery": "skipped",
            "token_cache_invalidation": "unknown",
            "schedulable": after_info.get("schedulable"),
            "scheduling_assessment": "paused" if after_info.get("schedulable") is False else "unknown",
            "remaining_blockers": ["narrow_sync_unavailable"]
            + (["schedulable_off"] if after_info.get("schedulable") is False else [])
            + (["status_error"] if str(after_info.get("status") or "").lower() == "error" else []),
            "partial": True,
            "recovery_mode_requested": recovery_mode,
            "after": after,
        }
        result["message"] = present_sync_message(result)
        return result

    if not narrow.get("ok"):
        result = {
            **narrow,
            "remote_account_id": remote_id,
            "recovery_mode_requested": recovery_mode,
        }
        result["message"] = present_sync_message(result)
        return result

    try:
        after = await sub2api_client.read_after_write(db, int(remote_id))
    except Exception as exc:  # noqa: BLE001
        result = {
            **narrow,
            "ok": False,
            "partial": True,
            "error_code": "final_get_failed",
            "error": f"同步后复读失败：{exc}",
            "recovery_mode_requested": recovery_mode,
        }
        result["message"] = present_sync_message(result)
        return result

    after_info = _identity_from_remote(after)
    result = {
        **narrow,
        "after": after,
        "schedulable": after_info.get("schedulable")
        if after_info.get("schedulable") is not None
        else narrow.get("schedulable"),
        "recovery_mode_requested": recovery_mode,
    }
    if result.get("schedulable") is False:
        blockers = list(result.get("remaining_blockers") or [])
        if "schedulable_off" not in blockers:
            blockers.append("schedulable_off")
        result["remaining_blockers"] = blockers
        result["scheduling_assessment"] = result.get("scheduling_assessment") or "paused"
    result["message"] = present_sync_message(result)
    return result
