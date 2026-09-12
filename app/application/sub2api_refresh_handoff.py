"""Drain remote refresh first, then accept the RT under the local writer lock."""
import json
from uuid import uuid4
from sqlalchemy import select, update, and_, or_
from app.core.time import utcnow
from app.persistence.models.identity import Account
from app.persistence.models.quota import CredentialLease
from app.persistence.models.oauth import OAuthSession
from app.persistence.models.sub2api import Sub2ApiRefreshAuthority
from app.persistence.models.refresh_handoff import Sub2ApiRefreshHandoff
from app.application.sub2api_refresh_authority import _context, _local, _fingerprint, _fingerprint_local, _encrypt, EXPECTED, failure
from app.application.sub2api_remote_state import _binding
from app.integrations.sub2api.refresh_handoff import handoff_call, handoff_failure


def _expected(ctx):
    return {"local_revision": ctx["local"]["credential_revision"], "local_fingerprint": _fingerprint_local(ctx["local"]),
            "remote_version": ctx["snapshot"]["credential_version"], "instance_id": ctx["snapshot"]["instance_id"],
            "binding_fingerprint": ctx["fingerprint"], "authority_epoch": (ctx["owner"] or {}).get("epoch", 0)}


async def preview_return(db, account_id, workspace_id=None):
    pending = await db.get(Sub2ApiRefreshHandoff, account_id, populate_existing=True)
    if pending:
        return {"ok": True, "resume": True, "operation_id": pending.operation_id,
                "message": "继续已有交接，不新建操作", "preconditions": {"operation_id": pending.operation_id}}
    ctx, error = await _context(db, account_id, workspace_id)
    if error:
        return error
    if not ctx["owner"]:
        return failure("authority_changed")
    return {"ok": True, "email": ctx["local"]["email"], "preconditions": _expected(ctx),
            "message": "将停止远端新刷新，等待在途结果落定，再把刷新凭据交回 Team；运行暂停和认证状态保持不变"}


async def _locked_local(db, account_id, revision, fingerprint, owner_epoch, binding_fingerprint, workspace_id):
    account = await db.get(Account, account_id, populate_existing=True)
    if account is None or account.credential_revision != revision or _fingerprint_local(_local(account)) != fingerprint:
        return None, failure("local_credentials_changed")
    original = _local(account)
    locked = await db.execute(update(Account).where(*[getattr(Account, k) == v for k, v in original.items()])
        .values(credential_revision=revision).execution_options(synchronize_session=False))
    if locked.rowcount != 1:
        return None, failure("local_credentials_changed")
    _, binding, code = await _binding(db, account_id, workspace_id)
    if code or _fingerprint(binding) != binding_fingerprint:
        return None, failure("binding_changed")
    owner = await db.get(Sub2ApiRefreshAuthority, account_id, populate_existing=True)
    if owner is None or owner.epoch != owner_epoch:
        return None, failure("authority_changed")
    lease = await db.get(CredentialLease, account_id, populate_existing=True)
    if lease and lease.token:
        return None, failure("refresh_in_flight")
    active = await db.scalar(select(OAuthSession.id).where(OAuthSession.account_id == account_id,
        OAuthSession.purpose == "account_reauth", or_(OAuthSession.status == "exchanging",
        and_(OAuthSession.status == "waiting", OAuthSession.expires_at > utcnow()))).limit(1))
    if active is not None:
        return None, failure("oauth_in_flight")
    return (account, owner, original), None


async def _acknowledge(db, account_id, remote_id, request):
    ack = await handoff_call(db, remote_id, request, "ack")
    confirmed = ack.get("ok") is True and ack.get("state") == "acknowledged"
    if confirmed:
        await db.execute(update(Sub2ApiRefreshHandoff).where(Sub2ApiRefreshHandoff.account_id == account_id,
            Sub2ApiRefreshHandoff.operation_id == request["operation_id"], Sub2ApiRefreshHandoff.state == "completed")
            .values(acknowledged=True).execution_options(synchronize_session=False))
        await db.commit()
    return {"ok": True, "owner": "team", "acknowledged": confirmed, "partial": not confirmed,
            "message": "已交回 Team；Sub2API 只保留访问令牌，暂停和认证状态保持不变" if confirmed else "归属已交回 Team，远端刷新已停止；交接暂存凭据的清理尚未确认，请点击确认交接收尾"}


async def return_to_team(db, account_id, expected, workspace_id=None):
    row = await db.get(Sub2ApiRefreshHandoff, account_id, populate_existing=True)
    if row is None:
        if not isinstance(expected, dict) or set(expected) != EXPECTED or any(type(expected.get(k)) is not int for k in ("local_revision", "remote_version", "authority_epoch")):
            return failure("precondition_missing")
        ctx, error = await _context(db, account_id, workspace_id)
        if error:
            return error
        if not ctx["owner"] or expected != _expected(ctx):
            return failure("authority_changed")
        locked, error = await _locked_local(db, account_id, expected["local_revision"], expected["local_fingerprint"],
            expected["authority_epoch"], expected["binding_fingerprint"], workspace_id)
        if error:
            await db.rollback()
            return error
        # The first local writer wins; repeated HTTP requests reuse this record.
        if await db.get(Sub2ApiRefreshHandoff, account_id, populate_existing=True):
            await db.rollback()
            return failure("authority_changed")
        request = {"operation_id": uuid4().hex, "expected_instance_id": ctx["snapshot"]["instance_id"],
                   "expected_credential_version": ctx["snapshot"]["credential_version"],
                   "expected_updated_at": ctx["snapshot"]["account_updated_at"],
                   "expected_identity": {"email": ctx["snapshot"]["identity"]["email"], "workspace_id": ctx["snapshot"]["identity"]["workspace_id"]}}
        row = Sub2ApiRefreshHandoff(account_id=account_id, operation_id=request["operation_id"], authority_epoch=expected["authority_epoch"],
            local_revision=expected["local_revision"], local_fingerprint=expected["local_fingerprint"], binding_fingerprint=expected["binding_fingerprint"],
            request_json=json.dumps(request), state="pending", updated_at=utcnow())
        db.add(row)
        await db.commit()
    elif expected != {"operation_id": row.operation_id}:
        return failure("precondition_missing")
    owner = await db.get(Sub2ApiRefreshAuthority, account_id, populate_existing=True)
    _, binding, code = await _binding(db, account_id, workspace_id)
    if code or _fingerprint(binding) != row.binding_fingerprint or owner is None or owner.epoch != row.authority_epoch:
        return failure("binding_changed")
    request = json.loads(row.request_json)
    remote_id = int(owner.remote_account_id)
    if row.state == "completed":
        return await _acknowledge(db, account_id, remote_id, request)
    locked, error = await _locked_local(db, account_id, row.local_revision, row.local_fingerprint, row.authority_epoch, row.binding_fingerprint, workspace_id)
    await db.rollback()  # No local write lock is held during remote I/O.
    if error:
        return error
    # SQLAlchemy rollback expires row objects; capture/reload explicit metadata.
    row = await db.get(Sub2ApiRefreshHandoff, account_id, populate_existing=True)
    frozen = {k: getattr(row, k) for k in ("local_revision", "local_fingerprint", "authority_epoch", "binding_fingerprint", "operation_id")}
    prepared = await handoff_call(db, remote_id, request, "prepare")
    if not prepared.get("ok"):
        return prepared
    if prepared["state"] != "ready":
        return {"ok": False, "pending": True, "operation_id": frozen["operation_id"], "message": "远端已阻止新刷新，正在等待在途结果；结果不明时不会因超时而放行，请稍后继续同一交接"}
    received = await handoff_call(db, remote_id, request, "read")
    if not received.get("ok"):
        return received
    if received["epoch"] != prepared["epoch"] or received["credential_version"] != prepared["credential_version"]:
        return handoff_failure("handoff_conflict")
    credentials = received.pop("credentials")
    candidate = {"access_token_encrypted": _encrypt(credentials["access_token"]), "refresh_token_encrypted": _encrypt(credentials["refresh_token"]),
                 "client_id": credentials["client_id"], "id_token_encrypted": None, "session_token_encrypted": None}
    locked, error = await _locked_local(db, account_id, frozen["local_revision"], frozen["local_fingerprint"], frozen["authority_epoch"], frozen["binding_fingerprint"], workspace_id)
    if error:
        await db.rollback()
        return error
    account, owner, original = locked
    row = await db.get(Sub2ApiRefreshHandoff, account_id, populate_existing=True)
    if row.state != "pending" or row.operation_id != frozen["operation_id"]:
        await db.rollback()
        return failure("authority_changed")
    revision = original["credential_revision"] + 1
    await db.execute(update(Account).where(Account.id == account_id).values(**candidate, credential_revision=revision, updated_at=utcnow()).execution_options(synchronize_session=False))
    epoch = owner.epoch + 1
    await db.execute(update(Sub2ApiRefreshAuthority).where(Sub2ApiRefreshAuthority.account_id == account_id).values(
        epoch=epoch, local_revision=revision, local_fingerprint=_fingerprint_local({**original, **candidate, "credential_revision": revision}),
        remote_version=received["credential_version"], last_success_at=utcnow(), last_attempt_at=utcnow(), last_error_code=None).execution_options(synchronize_session=False))
    row.state, row.authority_epoch, row.updated_at = "completed", epoch, utcnow()
    await db.commit()
    return {**await _acknowledge(db, account_id, remote_id, request), "authority_epoch": epoch}
