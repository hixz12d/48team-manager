"""Explicit validation and auth-only recovery; only Sub2API can attest validation."""
from uuid import uuid4

from app.application.operations import operation_store
from app.application.sub2api_refresh_authority import _context, _local, _fingerprint_local, EXPECTED, failure, preview_authority
from app.application.sub2api_remote_state import refresh_remote_state
from app.application.sub2api_publish import _build_credentials
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account


async def preview_auth_recovery(db, account_id, workspace_id=None):
    result = await preview_authority(db, account_id, workspace_id)
    if result.get("ok"):
        result["message"] = "将验证并推送本地新授权，只恢复有凭据版本证据的认证错误；暂停和额度限制不会解除"
    return result


async def recover_auth(db, account_id, expected, workspace_id=None):
    if (not isinstance(expected, dict) or set(expected) != EXPECTED
            or any(type(expected.get(k)) is not int for k in ("local_revision", "remote_version", "authority_epoch"))):
        return failure("precondition_missing")
    ctx, error = await _context(db, account_id, workspace_id)
    if error:
        return error
    snapshot = ctx["snapshot"]
    actual = {"local_revision": ctx["local"]["credential_revision"], "local_fingerprint": _fingerprint_local(ctx["local"]),
              "remote_version": snapshot["credential_version"], "instance_id": snapshot["instance_id"],
              "binding_fingerprint": ctx["fingerprint"], "authority_epoch": (ctx["owner"] or {}).get("epoch", 0)}
    if expected != actual:
        return failure("local_credentials_changed" if expected.get("local_fingerprint") != actual["local_fingerprint"] else "remote_changed")
    account = await db.get(Account, account_id, populate_existing=True)
    if account is None or _local(account) != ctx["local"]:
        return failure("local_credentials_changed")
    credentials = _build_credentials(account)
    if not account.client_id or not all(credentials.get(key) for key in ("access_token", "refresh_token", "client_id")):
        return {"ok": False, "message": "请先完成本地新授权，取得配套的访问令牌、刷新令牌和客户端信息", "error_code": "new_grant_required"}
    operation_id = uuid4().hex
    operation = await operation_store.create(db, op_type="sub2api_auth_recovery", account_id=account_id,
        email=account.email, workspace_id=workspace_id or 0,
        input_payload={"remote_account_id": ctx["remote_id"], "operation_id": operation_id, "local_revision": actual["local_revision"], "mode": "auth_only"})
    await db.commit()
    result = await sub2api_client.sync_oauth_credentials(db, ctx["remote_id"], credentials=credentials,
        expected_identity={"email": snapshot["identity"]["email"], "workspace_id": snapshot["identity"]["workspace_id"]},
        expected_updated_at=snapshot["account_updated_at"], expected_instance_id=snapshot["instance_id"],
        operation_id=operation_id, recovery_mode="auth_only")
    # All fields here have passed the client's narrow receipt parser. Never return
    # raw provider payloads or treat a local `auth_validated` flag as evidence.
    if result.get("auth_recovery") == "cleared":
        result["message"] = "新授权已通过身份与 Codex 服务访问验证，匹配的认证错误已清除；暂停、额度和其他阻断仍单独生效"
    else:
        result["message"] = result.get("error") or "结果尚未确认，请核对操作回执"
    await operation_store.finish(db, operation, {**result, "success": bool(result.get("ok"))})
    await db.commit()
    await refresh_remote_state(db, account_id, workspace_id)
    # Local auth state is deliberately untouched: remote validation cannot attest
    # a Team management endpoint or a concurrently changed local credential.
    return result
