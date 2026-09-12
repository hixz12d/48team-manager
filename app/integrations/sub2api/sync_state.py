"""Strict, redacted transport for integration state and follow-up retries."""
from datetime import datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx

BLOCKERS = {"schedulable_off", "account_error", "rate_limit", "temporary_pause", "overload", "account_expired", "final_state_unknown", "runtime_blocked"}
ERRORS = {"account_missing", "account_type_changed", "account_read_failed", "token_cache_failed", "scheduler_refresh_failed"}


def _when(value):
    if not isinstance(value, str):
        raise ValueError("invalid observation time")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("observation time requires timezone")
    return value


def _text(value, limit=255):
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("invalid metadata")
    return value


def parse_state(data: Any, remote_id: int) -> dict:
    if not isinstance(data, dict) or data.get("schema_version") != 1 or type(data.get("remote_account_id")) is not int or data["remote_account_id"] != remote_id:
        raise ValueError("invalid remote account")
    instance = str(UUID(data.get("instance_id", "")))
    if data.get("platform") != "openai" or data.get("type") != "oauth" or type(data.get("schedulable")) is not bool:
        raise ValueError("invalid account type")
    version = data.get("credential_version")
    if type(version) is not int or version < 0:
        raise ValueError("invalid credential version")
    identity = data.get("identity")
    if not isinstance(identity, dict):
        raise ValueError("identity missing")
    blockers = data.get("remaining_blockers")
    if not isinstance(blockers, list) or any(not isinstance(v, str) for v in blockers):
        raise ValueError("invalid blockers")
    op = data.get("latest_operation")
    clean_op = None
    if op is not None:
        if not isinstance(op, dict) or op.get("remote_account_id") != remote_id or op.get("state") not in {"pending", "completed", "needs_review"}:
            raise ValueError("invalid operation")
        key = _text(op.get("operation_id"), 128)
        if not key or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_." for c in key):
            raise ValueError("invalid operation id")
        op_version = op.get("credential_version")
        if type(op_version) is not int or op_version < 0 or op.get("credential_write") != "succeeded":
            raise ValueError("invalid write receipt")
        for field in ("token_cache_invalidation", "scheduler_refresh"):
            if op.get(field) not in {"pending", "succeeded"}:
                raise ValueError("invalid follow-up state")
        if op["state"] == "completed" and any(op[field] != "succeeded" for field in ("token_cache_invalidation", "scheduler_refresh")):
            raise ValueError("incomplete operation")
        clean_op = {"operation_id": key, "remote_account_id": remote_id, "state": op["state"], "credential_version": op_version,
                    "auth_recovery": op.get("auth_recovery") if op.get("auth_recovery") in {"cleared", "skipped"} else "skipped",
                    "validation_scope": op.get("validation_scope") if op.get("validation_scope") == "codex_identity_usage_catalog" else None,
                    "validated_at": _when(op["validated_at"]) if op.get("validation_scope") == "codex_identity_usage_catalog" and op.get("validated_at") else None,
                    "credential_write": "succeeded", "token_cache_invalidation": op["token_cache_invalidation"],
                    "scheduler_refresh": op["scheduler_refresh"], "updated_at": _when(op.get("updated_at")),
                    "created_at": _when(op.get("created_at")), "next_attempt_at": _when(op.get("next_attempt_at")),
                    "last_error": op.get("last_error") if op.get("last_error") in ERRORS else ""}
    return {"schema_version": 1, "instance_id": instance, "remote_account_id": remote_id,
            "platform": "openai", "type": "oauth", "credential_version": version,
            "identity": {key: _text(identity.get(key)) for key in ("email", "workspace_id", "official_account_id")},
            "account_updated_at": _when(data.get("account_updated_at")), "observed_at": _when(data.get("observed_at")),
            "schedulable": data["schedulable"], "status": data.get("status") if data.get("status") in {"active", "inactive", "disabled", "error"} else "unknown",
            "remaining_blockers": list(dict.fromkeys(v if v in BLOCKERS else "unknown_blocker" for v in blockers)),
            "availability": "not_verified", "latest_operation": clean_op,
            "access_token_readback": data.get("access_token_readback") is True,
            "operation_is_current": bool(clean_op and clean_op["credential_version"] == version)}


async def request_state(client, db, remote_id: int) -> dict:
    try:
        http, headers, _ = await client._with_client(db)
    except Exception:
        return {"ok": False, "error_code": "bridge_unavailable"}
    try:
        response = await http.get(f"/api/v1/admin/accounts/{remote_id}/credential-sync-state", headers=headers)
        if response.status_code in {401, 403}:
            return {"ok": False, "error_code": "bridge_admin_auth_failed"}
        if response.status_code == 404:
            return {"ok": False, "error_code": "state_or_account_missing"}
        response.raise_for_status()
        return {"ok": True, "snapshot": parse_state(client._unwrap_sync_payload(response.json()), remote_id)}
    except Exception:
        return {"ok": False, "error_code": "remote_state_unavailable"}
    finally:
        await http.aclose()


async def retry_followups(client, db, remote_id: int, operation_id: str, instance_id: str) -> dict:
    try:
        UUID(instance_id)
        http, headers, _ = await client._with_client(db)
    except Exception:
        return {"ok": False, "error_code": "bridge_unavailable"}
    try:
        response = await http.post(
            f"/api/v1/admin/accounts/{remote_id}/credential-sync-operations/{quote(operation_id, safe='')}/retry",
            headers=headers, json={"expected_instance_id": instance_id},
        )
        if response.status_code in {401, 403}:
            return {"ok": False, "error_code": "bridge_admin_auth_failed"}
        if response.status_code == 409:
            return {"ok": False, "error_code": "instance_mismatch"}
        response.raise_for_status()
        data = client._unwrap_sync_payload(response.json())
        receipt = data.get("receipt", {}) if isinstance(data, dict) else {}
        if data.get("state") != "recorded" or data.get("operation_id") != operation_id or receipt.get("remote_account_id") != remote_id:
            raise ValueError("invalid retry receipt")
        return {"ok": True, "queued": receipt.get("state") == "pending"}
    except (httpx.HTTPError, ValueError, TypeError, AttributeError):
        return {"ok": False, "error_code": "retry_outcome_unknown"}
    finally:
        await http.aclose()


async def request_access_token(client, db, remote_id, instance_id, version, updated_at):
    """Private server-to-server read. Callers must never serialize the returned AT."""
    try:
        http, headers, _ = await client._with_client(db)
    except Exception:
        return {"ok": False, "error_code": "remote_unavailable"}
    try:
        response = await http.post(f"/api/v1/admin/accounts/{remote_id}/credential-sync-access-token", headers=headers,
            json={"expected_instance_id": instance_id, "expected_credential_version": version, "expected_updated_at": updated_at})
        if response.status_code in {401, 403}:
            return {"ok": False, "error_code": "readback_auth_required"}
        if response.status_code in {404, 405}:
            return {"ok": False, "error_code": "readback_unsupported"}
        response.raise_for_status()
        data = client._unwrap_sync_payload(response.json())
        if (not isinstance(data, dict) or data.get("schema_version") != 1
                or data.get("instance_id") != instance_id or data.get("remote_account_id") != remote_id
                or type(data.get("credential_version")) is not int or data["credential_version"] != version
                or _when(data.get("account_updated_at")) != updated_at or data.get("refresh_configured") is not True):
            raise ValueError("invalid readback")
        if any(not isinstance(data.get(k), str) or not data[k] or data[k] != data[k].strip() or len(data[k]) > 65536 for k in ("access_token", "client_id")):
            raise ValueError("invalid readback")
        return {"ok": True, **{k: data[k] for k in ("instance_id", "remote_account_id", "credential_version", "account_updated_at", "refresh_configured", "access_token", "client_id")}}
    except Exception:
        return {"ok": False, "error_code": "remote_unavailable"}
    finally:
        await http.aclose()
