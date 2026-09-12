"""Opt-in remote refresh ownership. Only AT is copied; Team never consumes the RT."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import select, update

from app.application.sub2api_remote_state import _binding, _binding_identity, refresh_remote_state
from app.integrations.sub2api.sync_state import request_access_token
from app.core.crypto import token_cipher
from app.core.time import utcnow, as_utc
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account, ExternalBinding
from app.persistence.models.quota import CredentialLease
from app.persistence.models.sub2api import Sub2ApiRefreshAuthority

MESSAGES = {
    "precondition_missing": "请先核对远端状态，再确认采用远端凭据",
    "handoff_in_progress": "正在交回 Team，暂停接收远端访问令牌，请继续同一交接",
    "binding_changed": "绑定或实例已变化，停止接收远端凭据",
    "ambiguous_binding": "该账号存在多个远端绑定，暂不能委托刷新",
    "local_credentials_changed": "本地凭据已变化，请先核对新授权；没有覆盖本地凭据",
    "remote_changed": "远端版本已变化，请重新核对后再操作",
    "remote_refresh_pending": "等待 Sub2API 产生更新的访问令牌；Team 不会消费刷新令牌",
    "remote_unavailable": "远端凭据暂时无法确认，保留本地凭据并等待",
    "remote_refresh_not_configured": "远端缺少明确的刷新凭证或客户端信息，暂不能委托刷新",
    "refresh_in_flight": "本地刷新正在进行或结果尚未确认，暂不能切换归属",
    "oauth_in_flight": "本地授权尚未结束，暂不采用远端凭据；请完成或取消本次授权",
    "lease_lost": "本次处理的租约已失效，没有写入凭据",
    "authority_changed": "刷新归属已变化，请重新核对",
    "readback_unsupported": "Sub2API 尚不支持受控读取访问令牌，请先升级服务端",
    "readback_auth_required": "受控读取需要 Sub2API 管理员 API Key，请核对连接配置",
}
EXPECTED = {"local_revision", "local_fingerprint", "remote_version", "instance_id", "binding_fingerprint", "authority_epoch"}


def failure(code):
    return {"ok": False, "success": False, "allow_oauth": False, "error_code": code,
            "error": MESSAGES.get(code, "远端身份或状态未确认，没有修改本地凭据"),
            "message": MESSAGES.get(code, "远端身份或状态未确认，没有修改本地凭据")}


def _fingerprint(binding):
    return hashlib.sha256(json.dumps(_binding_identity(binding), separators=(",", ":")).encode()).hexdigest()


def _local(account):
    # Include encrypted fields as well as revision: legacy writers may not bump it.
    return {key: getattr(account, key) for key in (
        "id", "email", "official_account_id", "credential_revision", "client_id",
        "access_token_encrypted", "refresh_token_encrypted", "id_token_encrypted", "session_token_encrypted",
    )}


def _fingerprint_local(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def local_credential_fingerprint(account):
    return _fingerprint_local(_local(account))


def _encrypt(value):
    return token_cipher().encrypt(value)


async def authority_state(db, account_id):
    account = await db.get(Account, int(account_id), populate_existing=True)
    if account is None:
        return failure("binding_changed")
    owner = await db.get(Sub2ApiRefreshAuthority, account.id, populate_existing=True)
    from app.persistence.models.refresh_handoff import Sub2ApiRefreshHandoff
    handoff = await db.get(Sub2ApiRefreshHandoff, account_id, populate_existing=True)
    if owner and handoff and handoff.state == "completed" and handoff.authority_epoch == owner.epoch:
        return {"ok": True, "owner": "team", "returned": True, "authority_epoch": owner.epoch,
                "handoff_acknowledged": handoff.acknowledged,
                "local_revision": account.credential_revision, "message": "已安全交回 Team；远端只保留访问令牌" if handoff.acknowledged else "已交回 Team；交接暂存凭据清理待确认"}
    if owner is None:
        return {"ok": True, "owner": "team", "local_revision": account.credential_revision, "authority_epoch": 0}
    changed = account.credential_revision != owner.local_revision or local_credential_fingerprint(account) != owner.local_fingerprint
    code = "local_credentials_changed" if changed else owner.last_error_code
    return {"ok": True, "owner": "sub2api", "local_revision": account.credential_revision,
            "return_pending": bool(handoff and handoff.state == "pending"),
            "accepted_local_revision": owner.local_revision, "remote_version": owner.remote_version,
            "instance_id": owner.instance_id, "authority_epoch": owner.epoch,
            "last_success_at": as_utc(owner.last_success_at).isoformat(), "error_code": code,
            "message": MESSAGES.get(code, "Team 只接收访问令牌，日常刷新由 Sub2API 负责")}


async def _context(db, account_id, workspace_id):
    account, binding, code = await _binding(db, account_id, workspace_id)
    if code:
        return None, failure(code)
    count = list(await db.scalars(select(ExternalBinding.id).where(
        ExternalBinding.provider == "sub2api", ExternalBinding.local_account_id == account_id)))
    if len(count) != 1:
        return None, failure("ambiguous_binding")
    captured = {"local": _local(account), "fingerprint": _fingerprint(binding), "binding_id": binding.id,
                "remote_id": int(binding.remote_account_id), "workspace_id": binding.workspace_id}
    owner = await db.get(Sub2ApiRefreshAuthority, account_id, populate_existing=True)
    captured["owner"] = None if owner is None else {key: getattr(owner, key) for key in (
        "binding_id", "binding_fingerprint", "remote_account_id", "instance_id", "local_revision", "remote_version", "epoch",
        "local_fingerprint",
    )}
    state = await refresh_remote_state(db, account_id, workspace_id)
    if not state.get("ok") or state.get("stale") or not state.get("snapshot"):
        return None, failure(state.get("error_code") or "remote_unavailable")
    snapshot = state["snapshot"]
    if not snapshot.get("access_token_readback"):
        return None, failure("readback_unsupported")
    if snapshot["credential_version"] <= 0:
        return None, failure("remote_changed")
    # Identity is checked by refresh_remote_state; recheck the binding after its I/O.
    current, binding, code = await _binding(db, account_id, workspace_id)
    if code or _fingerprint(binding) != captured["fingerprint"]:
        return None, failure("binding_changed")
    if _local(current) != captured["local"]:
        return None, failure("local_credentials_changed")
    previous = captured["owner"]
    if previous and (previous["binding_fingerprint"] != captured["fingerprint"] or previous["instance_id"] != snapshot["instance_id"]):
        return None, failure("binding_changed")
    captured["snapshot"] = snapshot
    return captured, None


async def preview_authority(db, account_id, workspace_id=None):
    ctx, error = await _context(db, account_id, workspace_id)
    if error:
        return error
    return {"ok": True, "owner": "sub2api" if ctx["owner"] else "team", "email": ctx["local"]["email"],
            "remote_account_id": ctx["remote_id"], "remaining_blockers": ctx["snapshot"]["remaining_blockers"],
            "preconditions": {"local_revision": ctx["local"]["credential_revision"],
                "local_fingerprint": _fingerprint_local(ctx["local"]),
                "remote_version": ctx["snapshot"]["credential_version"], "instance_id": ctx["snapshot"]["instance_id"],
                "binding_fingerprint": ctx["fingerprint"], "authority_epoch": (ctx["owner"] or {}).get("epoch", 0)}}


async def _read_candidate(db, ctx):
    snapshot = ctx["snapshot"]
    try:
        result = await request_access_token(sub2api_client, db, ctx["remote_id"], snapshot["instance_id"], snapshot["credential_version"], snapshot["account_updated_at"])
        if not result.get("ok"):
            return None, failure(result.get("error_code") or "remote_unavailable")
        if (result.get("instance_id") != snapshot["instance_id"] or result.get("remote_account_id") != ctx["remote_id"]
                or result.get("credential_version") != snapshot["credential_version"]
                or result.get("account_updated_at") != snapshot["account_updated_at"] or result.get("refresh_configured") is not True):
            return None, failure("remote_changed")
        candidate = {"access_token_encrypted": _encrypt(result["access_token"]), "client_id": result["client_id"]}
        after = await refresh_remote_state(db, ctx["local"]["id"], ctx["workspace_id"])
        newer = after.get("snapshot") or {}
        if not after.get("ok") or newer.get("instance_id") != snapshot["instance_id"] or newer.get("credential_version") != snapshot["credential_version"] or newer.get("account_updated_at") != snapshot["account_updated_at"]:
            return None, failure("remote_changed")
        return candidate, None
    except Exception:
        return None, failure("remote_unavailable")


async def _commit_candidate(db, ctx, candidate, *, lease_ticket=None):
    original = ctx["local"]
    expected_owner = ctx["owner"]
    # This write takes SQLite's writer lock before rechecking the local lease/owner.
    guarded = await db.execute(update(Account).where(*[
        getattr(Account, key) == value for key, value in original.items()
    ]).values(credential_revision=original["credential_revision"]).execution_options(synchronize_session=False))
    if guarded.rowcount != 1:
        await db.rollback()
        return failure("local_credentials_changed")
    _, binding, code = await _binding(db, original["id"], ctx["workspace_id"])
    if code or _fingerprint(binding) != ctx["fingerprint"]:
        await db.rollback()
        return failure("binding_changed")
    bindings = list(await db.scalars(select(ExternalBinding.id).where(
        ExternalBinding.provider == "sub2api", ExternalBinding.local_account_id == original["id"])))
    if len(bindings) != 1:
        await db.rollback()
        return failure("ambiguous_binding")
    owner = await db.get(Sub2ApiRefreshAuthority, original["id"], populate_existing=True)
    if (owner.epoch if owner else 0) != ((expected_owner or {}).get("epoch", 0)):
        await db.rollback()
        return failure("authority_changed")
    from app.persistence.models.refresh_handoff import Sub2ApiRefreshHandoff
    handoff = await db.get(Sub2ApiRefreshHandoff, original["id"], populate_existing=True)
    if handoff and handoff.state == "pending":
        await db.rollback()
        return failure("handoff_in_progress")
    lease = await db.get(CredentialLease, original["id"], populate_existing=True)
    from app.persistence.models.oauth import OAuthSession
    from sqlalchemy import and_, or_
    active_oauth = await db.scalar(select(OAuthSession.id).where(
        OAuthSession.account_id == original["id"], OAuthSession.purpose == "account_reauth",
        or_(OAuthSession.status == "exchanging", and_(OAuthSession.status == "waiting", OAuthSession.expires_at > utcnow())),
    ).limit(1))
    if active_oauth is not None:
        await db.rollback()
        return failure("oauth_in_flight")
    if lease_ticket is None:
        if lease and lease.token:
            await db.rollback()
            return failure("refresh_in_flight")
    elif not lease or lease.token != lease_ticket or not lease.expires_at or as_utc(lease.expires_at) <= as_utc(utcnow()):
        await db.rollback()
        return failure("lease_lost")
    revision = int(original["credential_revision"]) + 1
    await db.execute(update(Account).where(Account.id == original["id"]).values(
        **candidate, refresh_token_encrypted=None, id_token_encrypted=None, session_token_encrypted=None,
        credential_revision=revision, updated_at=utcnow(),
    ).execution_options(synchronize_session=False))
    values = {"binding_id": ctx["binding_id"], "binding_fingerprint": ctx["fingerprint"],
              "instance_id": ctx["snapshot"]["instance_id"], "remote_account_id": str(ctx["remote_id"]),
              "remote_version": ctx["snapshot"]["credential_version"], "local_revision": revision,
              "local_fingerprint": _fingerprint_local({**original, **candidate, "refresh_token_encrypted": None,
                  "id_token_encrypted": None, "session_token_encrypted": None, "credential_revision": revision}),
              "epoch": (owner.epoch if owner else 0) + 1, "last_success_at": utcnow(), "last_attempt_at": utcnow(), "last_error_code": None}
    if owner:
        await db.execute(update(Sub2ApiRefreshAuthority).where(Sub2ApiRefreshAuthority.account_id == original["id"]).values(**values).execution_options(synchronize_session=False))
    else:
        db.add(Sub2ApiRefreshAuthority(account_id=original["id"], **values))
    await db.commit()
    await db.get(Account, original["id"], populate_existing=True)
    return {"ok": True, "success": True, "allow_oauth": False, "refreshed": True, "pulled": True,
            "owner": "sub2api", "local_revision": revision, "remote_version": values["remote_version"],
            "authority_epoch": values["epoch"],
            "message": "已接收远端访问令牌；日常刷新由 Sub2API 负责，账号可用性仍需单独验证"}


async def adopt_authority(db, account_id, expected, workspace_id=None):
    if not isinstance(expected, dict) or set(expected) != EXPECTED or any(type(expected.get(k)) is not int for k in ("local_revision", "remote_version", "authority_epoch")):
        return failure("precondition_missing")
    ctx, error = await _context(db, account_id, workspace_id)
    if error:
        return error
    actual = {"local_revision": ctx["local"]["credential_revision"], "remote_version": ctx["snapshot"]["credential_version"],
              "local_fingerprint": _fingerprint_local(ctx["local"]),
              "instance_id": ctx["snapshot"]["instance_id"], "binding_fingerprint": ctx["fingerprint"], "authority_epoch": (ctx["owner"] or {}).get("epoch", 0)}
    if expected != actual:
        if any(expected[k] != actual[k] for k in ("local_revision", "local_fingerprint")):
            return failure("local_credentials_changed")
        if expected["authority_epoch"] != actual["authority_epoch"]:
            return failure("authority_changed")
        return failure("remote_changed")
    if ctx["owner"] and ctx["snapshot"]["credential_version"] < ctx["owner"]["remote_version"]:
        return failure("remote_changed")
    candidate, error = await _read_candidate(db, ctx)
    return error if error else await _commit_candidate(db, ctx, candidate)


async def pull_owned_access_token(db, account_id, lease_ticket):
    """Called only inside AuthService's credential lease; all failures prohibit OAuth fallback."""
    owner = await db.get(Sub2ApiRefreshAuthority, account_id, populate_existing=True)
    from app.persistence.models.refresh_handoff import Sub2ApiRefreshHandoff
    handoff = await db.get(Sub2ApiRefreshHandoff, account_id, populate_existing=True)
    if handoff and handoff.state == "pending":
        return failure("handoff_in_progress")
    if owner is None:
        return failure("authority_changed")
    initial_epoch = owner.epoch
    binding = await db.get(ExternalBinding, owner.binding_id, populate_existing=True)
    if binding is None:
        await db.execute(update(Sub2ApiRefreshAuthority).where(
            Sub2ApiRefreshAuthority.account_id == account_id, Sub2ApiRefreshAuthority.epoch == initial_epoch,
        ).values(last_error_code="binding_changed", last_attempt_at=utcnow()).execution_options(synchronize_session=False))
        await db.commit()
        return failure("binding_changed")
    ctx, error = await _context(db, account_id, binding.workspace_id)
    if not error and (not ctx["owner"] or ctx["owner"]["epoch"] != initial_epoch):
        error = failure("authority_changed")
    if not error and (ctx["local"]["credential_revision"] != ctx["owner"]["local_revision"] or _fingerprint_local(ctx["local"]) != ctx["owner"]["local_fingerprint"]):
        error = failure("local_credentials_changed")
    if not error and ctx["snapshot"]["credential_version"] <= ctx["owner"]["remote_version"]:
        error = failure("remote_refresh_pending" if ctx["snapshot"]["credential_version"] == ctx["owner"]["remote_version"] else "remote_changed")
    if not error:
        candidate, error = await _read_candidate(db, ctx)
        if not error:
            result = await _commit_candidate(db, ctx, candidate, lease_ticket=lease_ticket)
            if result.get("ok"):
                return result
            error = result
    await db.execute(update(Sub2ApiRefreshAuthority).where(
        Sub2ApiRefreshAuthority.account_id == account_id, Sub2ApiRefreshAuthority.epoch == initial_epoch,
    ).values(last_error_code=error["error_code"], last_attempt_at=utcnow()).execution_options(synchronize_session=False))
    await db.commit()
    return error
