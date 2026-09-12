"""Verified binding observations and retries of propagation only."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.domain.identity.ids import normalize_email
from app.integrations.sub2api.client import sub2api_client
from app.integrations.sub2api.sync_state import parse_state, request_state, retry_followups
from app.persistence.models.identity import Account, ExternalBinding
from app.persistence.models.sub2api import Sub2ApiSyncObservation

MESSAGES = {
    "not_bound": "没有已验证的远端绑定，请先核对绑定",
    "ambiguous_binding": "存在多个工作区绑定，请从对应团队的账号详情核对",
    "identity_mismatch": "远端账号身份与当前绑定不一致，已停止后续操作",
    "instance_mismatch": "Sub2API 实例已变化，保留原快照，请先人工核对连接和绑定",
    "binding_changed": "核对期间绑定发生变化，请重新打开账号详情",
    "bridge_admin_auth_failed": "Sub2API 管理认证失败，请检查管理密钥",
    "state_or_account_missing": "远端状态接口或账号不存在，请核对服务版本和绑定",
    "remote_state_unavailable": "本次未能读取远端状态，旧快照仍保留",
    "bridge_unavailable": "无法连接 Sub2API，旧快照仍保留",
    "retry_outcome_unknown": "重试请求结果未确认，请重新核对状态",
    "nothing_to_retry": "当前没有可重试的后续步骤",
    "older_observation": "收到较旧的远端状态，已保留较新的快照",
}


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _error(code):
    return {"ok": False, "error_code": code, "message": MESSAGES.get(code, "远端状态未确认，请重新核对")}


async def _binding(db, account_id, workspace_id=None):
    account = await db.get(Account, int(account_id), populate_existing=True)
    if account is None:
        return None, None, "not_bound"
    query = select(ExternalBinding).where(ExternalBinding.provider == "sub2api", ExternalBinding.local_account_id == account.id)
    if workspace_id is not None:
        query = query.where(ExternalBinding.workspace_id == int(workspace_id))
    rows = list((await db.scalars(query.execution_options(populate_existing=True))).all())
    if len(rows) > 1:
        return account, None, "ambiguous_binding"
    if not rows or rows[0].binding_state != "verified" or not str(rows[0].remote_account_id).isdigit():
        return account, None, "not_bound"
    return account, rows[0], None


def _binding_identity(binding):
    return (binding.id, binding.local_account_id, binding.workspace_id, binding.remote_account_id, binding.binding_state,
            binding.verified_email, binding.verified_official_account_id, binding.verified_workspace_id)


async def pinned_sync_instance(db, binding):
    if binding is not None:
        from app.persistence.models.sub2api import Sub2ApiRefreshAuthority
        import hashlib
        owner = await db.get(Sub2ApiRefreshAuthority, binding.local_account_id, populate_existing=True)
        if owner:
            fingerprint = hashlib.sha256(json.dumps(_binding_identity(binding), separators=(",", ":")).encode()).hexdigest()
            if owner.binding_fingerprint != fingerprint:
                raise RuntimeError("远端刷新归属的绑定已变化，请先核对")
            return owner.instance_id
    if binding is None:
        return None
    row = await db.get(Sub2ApiSyncObservation, binding.id, populate_existing=True)
    if row and row.remote_account_id != str(binding.remote_account_id):
        raise RuntimeError("远端绑定已变化，请先重新核对绑定和实例")
    if row:
        return row.instance_id or None
    return None


async def cached_remote_state(db, account_id, workspace_id=None):
    _, binding, code = await _binding(db, account_id, workspace_id)
    if code:
        return _error(code)
    row = await db.get(Sub2ApiSyncObservation, binding.id, populate_existing=True)
    if row is not None and row.remote_account_id != str(binding.remote_account_id):
        return _error("binding_changed")
    if row is None:
        return {"ok": True, "state": "not_checked", "stale": True, "can_retry": False, "snapshot": None}
    snapshot = json.loads(row.snapshot_json)
    stale = bool(row.last_error_code or not snapshot or (_now() - _utc(row.checked_at)).total_seconds() > 120)
    op = (snapshot or {}).get("latest_operation") or {}
    state = "needs_review" if row.last_error_code else op.get("state") or "unrecorded"
    return {"ok": not bool(row.last_error_code), "state": state, "snapshot": snapshot, "stale": stale,
            "checked_at": _utc(row.checked_at).isoformat() if snapshot else None,
            "last_attempt_at": _utc(row.last_error_at or row.checked_at).isoformat(),
            "error_code": row.last_error_code, "message": MESSAGES.get(row.last_error_code, ""),
            "can_retry": bool(not stale and op.get("state") == "pending" and (snapshot or {}).get("operation_is_current")),
            "binding_id": binding.id, "observation_revision": row.revision}


def _older(incoming, previous):
    if not previous:
        return False
    if incoming["credential_version"] < previous["credential_version"]:
        return True
    if _utc(incoming["account_updated_at"]) < _utc(previous["account_updated_at"]):
        return True
    old_op = previous.get("latest_operation") or {}
    new_op = incoming.get("latest_operation") or {}
    if old_op and not new_op:
        return True
    if old_op and new_op and new_op["credential_version"] < old_op["credential_version"]:
        return True
    if old_op and new_op and new_op["operation_id"] == old_op["operation_id"] and _utc(new_op["updated_at"]) < _utc(old_op["updated_at"]):
        return True
    return False


async def _save(db, binding, old, snapshot=None, code=None):
    now = _now()
    if old is not None:
        values = {"revision": old.revision + 1, "last_error_code": code, "last_error_at": now if code else None}
        if snapshot is not None:
            values.update(instance_id=snapshot["instance_id"], snapshot_json=json.dumps(snapshot, ensure_ascii=False), checked_at=now)
        # A slower response must never overwrite a concurrently committed observation.
        await db.execute(update(Sub2ApiSyncObservation).where(
            Sub2ApiSyncObservation.binding_id == binding.id, Sub2ApiSyncObservation.revision == old.revision,
        ).values(**values).execution_options(synchronize_session=False))
    else:
        try:
            async with db.begin_nested():
                db.add(Sub2ApiSyncObservation(binding_id=binding.id, remote_account_id=str(binding.remote_account_id),
                    instance_id=(snapshot or {}).get("instance_id", ""), snapshot_json=json.dumps(snapshot, ensure_ascii=False),
                    revision=1, checked_at=now, last_error_code=code, last_error_at=now if code else None))
                await db.flush()
        except IntegrityError:
            # Another observation pinned this binding first. Its state wins.
            pass
    await db.commit()


async def refresh_remote_state(db, account_id, workspace_id=None):
    account, binding, code = await _binding(db, account_id, workspace_id)
    if code:
        return _error(code)
    identity_before = _binding_identity(binding)
    old = await db.get(Sub2ApiSyncObservation, binding.id, populate_existing=True)
    expected_remote_id = int(binding.remote_account_id)
    result = await request_state(sub2api_client, db, expected_remote_id)
    _, current, code = await _binding(db, account_id, workspace_id)
    if code or _binding_identity(current) != identity_before:
        return _error("binding_changed")
    if not result.get("ok"):
        await _save(db, binding, old, code=result.get("error_code") or "remote_state_unavailable")
        return await cached_remote_state(db, account_id, workspace_id)
    try:
        snapshot = parse_state(result["snapshot"], expected_remote_id)
        from app.application.sub2api_publish import _expected_workspace
        expected_workspace = await _expected_workspace(db, account, binding.workspace_id)
        remote_identity = snapshot["identity"]
        expected_official = binding.verified_official_account_id or account.official_account_id
        mismatch = (normalize_email(remote_identity["email"]) != normalize_email(account.email)
                    or str(remote_identity["workspace_id"] or "").lower() != str(expected_workspace or "").lower()
                    or bool(expected_official and remote_identity["official_account_id"] != expected_official))
        code = "identity_mismatch" if mismatch else None
        if old and old.instance_id and (snapshot["instance_id"] != old.instance_id or old.remote_account_id != str(expected_remote_id)):
            code = "instance_mismatch"
        if code is None and old and _older(snapshot, json.loads(old.snapshot_json)):
            code = "older_observation"
    except (ValueError, KeyError, TypeError):
        snapshot, code = None, "remote_state_unavailable"
    await _save(db, binding, old, snapshot=snapshot if code is None else None, code=code)
    return await cached_remote_state(db, account_id, workspace_id)


async def retry_remote_followups(db, account_id, workspace_id=None):
    # Always verify again immediately before issuing the narrow retry request.
    current = await refresh_remote_state(db, account_id, workspace_id)
    if not current.get("ok") or not current.get("can_retry"):
        return current if not current.get("ok") else {**current, **_error("nothing_to_retry")}
    snapshot = current["snapshot"]
    op = snapshot["latest_operation"]
    result = await retry_followups(sub2api_client, db, snapshot["remote_account_id"], op["operation_id"], snapshot["instance_id"])
    if not result.get("ok"):
        _, binding, code = await _binding(db, account_id, workspace_id)
        if not code:
            old = await db.get(Sub2ApiSyncObservation, binding.id, populate_existing=True)
            await _save(db, binding, old, code=result.get("error_code") or "retry_outcome_unknown")
        return {**current, **_error(result.get("error_code") or "retry_outcome_unknown"), "can_retry": False, "stale": True}
    refreshed = await refresh_remote_state(db, account_id, workspace_id)
    return {**refreshed, "message": "已请求继续处理未完成步骤；凭据未重新提交" if refreshed.get("ok") else refreshed.get("message")}
