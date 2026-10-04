"""codex-rs import: AT + RT + ID token go to codex-rs, which then owns refresh.

Every public function here never raises; callers rely on the documented return shapes.
Token values never appear in return values, logs, or exception messages, and the RT
that codex-rs exports is dropped by the client before it reaches this module.
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid
from datetime import timedelta

from sqlalchemy import case, or_, select, update
from sqlalchemy.dialects.sqlite import insert

from app.application.codex_export import CodexTransferError, account_document
from app.application.refresh_ownership import remote_refresh_owner
from app.application.settings import SECRET_MASK, get_setting_value, load_codex_rs_import_defaults
from app.application.tokens import decrypt_secret, encrypt_secret
from app.core.jwt import jwt_parser
from app.core.time import as_utc, isoformat, utcnow
from app.domain.identity import MEMBERSHIP_STATE_JOINED
from app.integrations.codex.client import CodexClient, load_config, normalize_url
from app.persistence.models.codex import CodexBinding
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from app.persistence.models.quota import CredentialLease

logger = logging.getLogger(__name__)

LEASE_SECONDS = 180
IMPORT_TIMEOUT_SECONDS = 120
MAX_BATCH = 50
# Sub2API bindings already confirmed gone remotely do not block an import.
SUB2API_GONE_STATES = ("missing", "orphaned")
PULLABLE_STATES = ("synced", "uncertain")
# Client error codes for HTTP 4xx answers: codex-rs refused the request and stored nothing.
DEFINITE_REJECTIONS = {"codex_rejected", "codex_auth_failed", "remote_not_found"}
# Local fields that must stay untouched between reading and writing credentials.
GUARD_FIELDS = ("email", "official_account_id", "credential_revision", "client_id", "access_token_encrypted",
                "refresh_token_encrypted", "id_token_encrypted", "session_token_encrypted")
NOT_CONFIGURED = "请先保存 codex-rs 地址和管理员 API Key"

MESSAGES = {
    "not_found": "账号不存在",
    "mother_account": "母号不导入 codex-rs",
    "refresh_token_missing": "缺少刷新令牌，请先重新授权",
    "credential_error": "刷新令牌无法解密，请先重新授权",
    "sub2api_bound": "该号已在 Sub2API，先在 Sub2API 删除后再导入 codex-rs",
    "target_changed": "该号已绑定其他 codex-rs 地址，未向新地址发送凭据",
    "push_busy": "该号已有 codex-rs 导入在进行中",
    "refresh_in_flight": "本地刷新正在进行或结果未确认，请稍后再导入",
    "no_proxy": "codex-rs 没有测试通过的代理，未导入",
    "remote_identity_mismatch": "codex-rs 中的账号身份或刷新令牌与本地不一致，请人工核对",
    "local_credentials_changed": "导入期间本地凭据已变化，请重新导入",
    "lease_lost": "本次导入的租约已失效，没有写入本地",
    "import_timeout": "导入超时，结果不明；可直接重试（codex-rs 按身份覆盖同一账号）",
    "import_failed": "导入失败，请稍后重试",
    "too_many": "单次最多导入 50 个账号",
}
PULL_MESSAGES = {
    "codex_binding_missing": "该号没有已同步的 codex-rs 绑定，无法读回访问令牌",
    "remote_identity_mismatch": "codex-rs 返回的令牌身份与本地不一致，没有写入",
    "remote_refresh_pending": "等待 codex-rs 产生更新的访问令牌",
    "lease_lost": "本次处理的租约已失效，没有写入凭据",
    "local_credentials_changed": "本地凭据已变化，没有覆盖",
    "remote_unavailable": "codex-rs 暂时无法读取，保留本地凭据并等待",
}


def _error(code: str) -> CodexTransferError:
    return CodexTransferError(code, MESSAGES.get(code, MESSAGES["import_failed"]))


def _result(account_id: int, ok: bool, state, remote_id, code, message: str) -> dict:
    return {"account_id": account_id, "ok": ok, "state": state, "remote_account_id": remote_id,
            "error_code": code, "message": message}


def _pull_failure(code: str) -> dict:
    message = PULL_MESSAGES.get(code, PULL_MESSAGES["remote_unavailable"])
    return {"ok": False, "success": False, "allow_oauth": False, "error_code": code, "error": message, "message": message}


def _snapshot(account: Account) -> dict:
    return {key: getattr(account, key) for key in GUARD_FIELDS}


async def _rollback(db) -> None:
    """Roll back, then reload the caller's ORM objects.

    A rollback expires every instance in the session, and callers (handoff, rotate, sync)
    keep using theirs afterwards; under async SQLAlchemy an expired attribute cannot lazy-load.
    """
    kept = list(db.identity_map.values())
    if not db.in_transaction():
        return
    await db.rollback()
    for obj in kept:
        try:
            await db.refresh(obj)
        except Exception:  # noqa: BLE001 - deleted rows simply stay expired
            pass
    await db.commit()


async def _guard(db, account_id: int, snapshot: dict) -> bool:
    """Take SQLite's write lock and confirm local credentials are exactly as read before I/O."""
    result = await db.execute(update(Account).where(
        Account.id == account_id, *[getattr(Account, key) == value for key, value in snapshot.items()],
    ).values(credential_revision=snapshot["credential_revision"]).execution_options(synchronize_session=False))
    return result.rowcount == 1


async def _client(db) -> CodexClient:
    try:
        config = await load_config(db)
    except ValueError:
        raise CodexTransferError("codex_not_configured", "已保存的 codex-rs 地址无效，请重新保存") from None
    return CodexClient(config["base_url"], config["api_key"])


def _token_identity(token: str) -> dict | None:
    claims = jwt_parser.decode_token(token)
    if not isinstance(claims, dict):
        return None
    auth = claims.get("https://api.openai.com/auth") or {}
    profile = claims.get("https://api.openai.com/profile") or {}
    if not isinstance(auth, dict) or not isinstance(profile, dict):
        return None
    return {"email": str(profile.get("email") or claims.get("email") or "").strip().lower() or None,
            "account_id": auth.get("chatgpt_account_id") or None,
            "user_id": auth.get("chatgpt_user_id") or auth.get("user_id") or None}


async def _check_exclusive(db, account_id: int) -> None:
    bound = await db.scalar(select(ExternalBinding.id).where(
        ExternalBinding.provider == "sub2api", ExternalBinding.local_account_id == account_id,
        ExternalBinding.binding_state.not_in(SUB2API_GONE_STATES)).limit(1))
    if bound is not None or await remote_refresh_owner(db, account_id) is not None:
        raise _error("sub2api_bound")


def _pick_proxy(proxies: list[dict]) -> str:
    """Among proxies whose last test succeeded, pick randomly from those with the fewest accounts."""
    def load(item):
        value = item.get("accountCount")
        return value if type(value) is int and value >= 0 else 2 ** 63

    tested = [item for item in proxies if isinstance(item.get("lastTest"), dict)
              and item["lastTest"].get("success") is True and isinstance(item.get("id"), str) and item["id"]]
    if not tested:
        raise _error("no_proxy")
    least = min(load(item) for item in tested)
    return random.choice([item for item in tested if load(item) == least])["id"]


def _verify_remote(remote: dict, identity: dict) -> None:
    if (str(remote.get("email") or "").strip().lower() != identity["email"]
            or remote.get("accountId") != identity["account_id"]
            or remote.get("userId") != identity["user_id"]
            or remote.get("hasRefreshToken") is not True):
        raise _error("remote_identity_mismatch")


async def _in_team(db, account_id: int) -> bool:
    return await db.scalar(select(WorkspaceMembership.id).where(
        WorkspaceMembership.account_id == account_id,
        WorkspaceMembership.membership_state == MEMBERSHIP_STATE_JOINED).limit(1)) is not None


async def _prepare(db, account_id: int, target_url: str) -> dict:
    """Validate locally and claim the binding lease in one write transaction."""
    # Serialize against deletion and concurrent credential writers.
    await db.execute(update(Account).where(Account.id == account_id).values(version=Account.version))
    account = await db.get(Account, account_id, populate_existing=True)
    if account is None:
        raise _error("not_found")
    owns_team = await db.scalar(select(Workspace.id).where(Workspace.owner_account_id == account_id).limit(1))
    if account.local_purpose == "mother" or owns_team is not None:
        raise _error("mother_account")
    document = account_document(account)
    if not account.refresh_token_encrypted:
        raise _error("refresh_token_missing")
    refresh = decrypt_secret(account.refresh_token_encrypted)
    if not refresh:
        raise _error("credential_error")
    identity = _token_identity(document["access_token"]) or {}
    identity["email"] = document["email"]
    await _check_exclusive(db, account_id)
    await db.execute(insert(CodexBinding).values(
        account_id=account_id, target_url=target_url,
        remote_name=f"team48:{uuid.uuid4().hex}:{account.email}"[:255], state="pending",
        import_attempted=False).on_conflict_do_nothing(index_elements=["account_id"]))
    binding = await db.get(CodexBinding, account_id, populate_existing=True)
    if binding.target_url != target_url:
        raise _error("target_changed")
    previous_state = binding.state
    first = not binding.remote_account_id
    ticket = uuid.uuid4().hex
    now = utcnow()
    claim = await db.execute(update(CodexBinding).where(
        CodexBinding.account_id == account_id,
        or_(CodexBinding.lease_until.is_(None), CodexBinding.lease_until <= now),
    ).values(lease_token=ticket, lease_until=now + timedelta(seconds=LEASE_SECONDS), state="pushing")
        .execution_options(synchronize_session=False))
    if claim.rowcount != 1:
        raise _error("push_busy")
    prepared = {"ticket": ticket, "previous_state": previous_state, "first": first, "identity": identity,
                "was_attempted": bool(binding.import_attempted),
                "document": {**document, "refresh_token": refresh}, "snapshot": _snapshot(account)}
    await db.commit()
    return prepared


async def _mark_attempted(db, account_id: int, ticket: str, snapshot: dict) -> None:
    """Last local checkpoint before the RT leaves: from here on codex-rs may own refresh."""
    if not await _guard(db, account_id, snapshot):
        raise _error("local_credentials_changed")
    await _check_exclusive(db, account_id)
    lease = await db.get(CredentialLease, account_id, populate_existing=True)
    if lease is not None and lease.token:
        raise _error("refresh_in_flight")
    marked = await db.execute(update(CodexBinding).where(
        CodexBinding.account_id == account_id, CodexBinding.lease_token == ticket,
    ).values(import_attempted=True).execution_options(synchronize_session=False))
    if marked.rowcount != 1:
        raise _error("lease_lost")
    await db.commit()


async def _commit_import(db, account_id: int, ticket: str, snapshot: dict, remote_id: str,
                         workspace: str | None, enabled, note: str | None) -> None:
    if not await _guard(db, account_id, snapshot):
        raise _error("local_credentials_changed")
    binding = await db.get(CodexBinding, account_id, populate_existing=True)
    if binding is None or binding.lease_token != ticket:
        raise _error("lease_lost")
    revision = int(snapshot["credential_revision"] or 1) + 1
    now = utcnow()
    # codex-rs now owns the RT: drop the local copy so no path can consume it again.
    await db.execute(update(Account).where(Account.id == account_id).values(
        refresh_token_encrypted=None, session_token_encrypted=None, credential_revision=revision, updated_at=now,
    ).execution_options(synchronize_session=False))
    await db.execute(update(CodexBinding).where(CodexBinding.account_id == account_id).values(
        state="synced", remote_account_id=remote_id, official_workspace_id=workspace,
        remote_enabled=enabled if isinstance(enabled, bool) else None, credential_revision=revision,
        synced_at=now, last_error=note,
    ).execution_options(synchronize_session=False))
    await db.commit()
    # Callers (e.g. the post-authorization handoff) keep using their Account instance.
    await db.get(Account, account_id, populate_existing=True)
    await db.commit()


async def _record_failure(db, account_id: int, ticket: str, *, sent: bool, released: bool, previous_state: str,
                          code: str, verified: tuple[str, str | None] | None) -> tuple[str | None, str | None]:
    binding = await db.get(CodexBinding, account_id, populate_existing=True)
    if binding is None or binding.lease_token != ticket:
        return None, None
    if not binding.remote_account_id and (released or not sent and not binding.import_attempted):
        # Nothing ever reached codex-rs: drop the placeholder so Team48 keeps refreshing locally.
        await db.delete(binding)
        await db.commit()
        return None, None
    state = "uncertain" if sent or previous_state == "pushing" else previous_state
    values = {"state": state, "last_error": code[:80], "lease_token": None, "lease_until": None}
    if verified is not None:
        values.update(remote_account_id=verified[0], official_workspace_id=verified[1])
    await db.execute(update(CodexBinding).where(CodexBinding.account_id == account_id).values(**values)
                     .execution_options(synchronize_session=False))
    await db.commit()
    return state, values.get("remote_account_id", binding.remote_account_id)


async def _import(db, account_id: int) -> dict:
    try:
        client = await _client(db)
        prepared = await _prepare(db, account_id, client.base_url)
    except CodexTransferError as exc:
        await _rollback(db)
        return _result(account_id, False, None, None, exc.code, str(exc))
    ticket, first, identity = prepared["ticket"], prepared["first"], prepared["identity"]
    sent = imported = False
    verified = None
    try:
        async with asyncio.timeout(IMPORT_TIMEOUT_SECONDS):
            settings = proxy_id = None
            if first:
                # Also proves the admin key works before anything is marked as handed over.
                proxy_id = _pick_proxy(await client.list_proxies())
                settings = await load_codex_rs_import_defaults(db)
            await _mark_attempted(db, account_id, ticket, prepared["snapshot"])
            sent = True
            remote_id = (await client.import_accounts(prepared["document"], settings=settings, proxy_id=proxy_id))[0]
            imported = True
            remote = await client.detail(remote_id)
            _verify_remote(remote, identity)
            verified = (remote_id, identity["account_id"])
            enabled, note = remote.get("enabled"), None
            # Re-enable an account that was disabled when it left a team, once it is back in one.
            back_in_team = not first and enabled is False and await _in_team(db, account_id)
            await db.commit()  # never hold a read transaction across network I/O
            if back_in_team:
                try:
                    await client.set_enabled(remote_id, True)
                    enabled = (await client.detail(remote_id)).get("enabled")
                except CodexTransferError:
                    pass
                if enabled is not True:
                    note = "codex_rs_enable_failed"
            await _commit_import(db, account_id, ticket, prepared["snapshot"], remote_id,
                                 identity["account_id"], enabled, note)
        message = "已导入 codex-rs，续期由 codex-rs 负责"
        if note:
            message += "；但重新启用失败，请到 codex-rs 后台手动启用"
        return _result(account_id, True, "synced", remote_id, None, message)
    except (CodexTransferError, TimeoutError) as exc:
        await _rollback(db)
        error = exc if isinstance(exc, CodexTransferError) else _error("import_timeout")
        # A definite 4xx on a first-ever import means codex-rs stored nothing (it rolls back whole imports).
        released = (first and sent and not imported and not prepared["was_attempted"]
                    and error.code in DEFINITE_REJECTIONS)
        state, remote_id = await _record_failure(db, account_id, ticket, sent=sent, released=released,
                                                 previous_state=prepared["previous_state"],
                                                 code=error.code, verified=verified)
        logger.warning("codex-rs 导入失败 account_id=%s error=%s", account_id, error.code)
        return _result(account_id, False, state, remote_id, error.code, str(error))
    finally:
        await _rollback(db)
        await db.execute(update(CodexBinding).where(
            CodexBinding.account_id == account_id, CodexBinding.lease_token == ticket,
        ).values(lease_token=None, lease_until=None,
                 state=case((CodexBinding.state == "pushing", "uncertain"), else_=CodexBinding.state))
            .execution_options(synchronize_session=False))
        await db.commit()


async def import_account(db, account_id: int) -> dict:
    """Import one account (with RT) into codex-rs. Never raises.

    Returns {"account_id", "ok", "state", "remote_account_id", "error_code", "message"}.
    ``state`` is the binding state after the attempt (pending / pushing / synced / uncertain),
    or None when no binding was touched.
    """
    account_id = int(account_id)
    try:
        return await _import(db, account_id)
    except Exception as exc:  # noqa: BLE001 - callers rely on a result, never an exception
        await _rollback(db)
        logger.warning("codex-rs 导入异常 account_id=%s error=%s", account_id, type(exc).__name__)
        return _result(account_id, False, None, None, "import_failed", MESSAGES["import_failed"])


async def import_accounts(db, account_ids: list[int]) -> dict:
    """Deduplicate, then import one by one (max 50). Partial success is allowed.

    Returns {"ok": all ok, "synced": success count, "results": [import_account(...) items]}.
    """
    ids = list(dict.fromkeys(int(item) for item in account_ids))
    if len(ids) > MAX_BATCH:
        return {"ok": False, "synced": 0, "results": [], "error_code": "too_many", "message": MESSAGES["too_many"]}
    results = [await import_account(db, account_id) for account_id in ids]
    return {"ok": bool(results) and all(item["ok"] for item in results),
            "synced": sum(1 for item in results if item["ok"]), "results": results}


async def _pull(db, account_id: int, lease_ticket: str) -> dict:
    binding = await db.get(CodexBinding, account_id, populate_existing=True)
    account = await db.get(Account, account_id, populate_existing=True)
    # A failed re-import leaves "uncertain" with a verified remote ID; codex-rs still owns that account.
    if binding is None or binding.state not in PULLABLE_STATES or not binding.remote_account_id or account is None:
        return _pull_failure("codex_binding_missing")
    remote_id = binding.remote_account_id
    expected = {"email": str(account.email or "").strip().lower(),
                "account_id": binding.official_workspace_id or account.official_account_id,
                "user_id": account.official_user_id}
    snapshot = _snapshot(account)
    local_access = decrypt_secret(account.access_token_encrypted)
    local_exp = jwt_parser.expiration_utc(local_access) if local_access else None
    try:
        client = await _client(db)
        await db.commit()  # end the read transaction before network I/O
        tokens = await client.export_tokens(remote_id)
    except CodexTransferError:
        return _pull_failure("remote_unavailable")
    access, id_token = tokens["access_token"], tokens.get("id_token")
    identity = _token_identity(access)
    if (identity is None or identity["email"] != expected["email"]
            or identity["account_id"] != expected["account_id"]
            or expected["user_id"] and identity["user_id"] != expected["user_id"]
            or tokens.get("email") and tokens["email"].strip().lower() != expected["email"]
            or tokens.get("account_id") and tokens["account_id"] != identity["account_id"]
            or tokens.get("user_id") and tokens["user_id"] != identity["user_id"]):
        return _pull_failure("remote_identity_mismatch")
    if id_token:
        id_identity = _token_identity(id_token)
        if (id_identity is None or id_identity["email"] not in (None, expected["email"])
                or id_identity["account_id"] not in (None, identity["account_id"])
                or id_identity["user_id"] not in (None, identity["user_id"])):
            return _pull_failure("remote_identity_mismatch")
    remote_exp = jwt_parser.expiration_utc(access)
    if remote_exp is None or remote_exp <= utcnow() or (local_exp is not None and remote_exp <= local_exp):
        return _pull_failure("remote_refresh_pending")
    if not await _guard(db, account_id, snapshot):
        await _rollback(db)
        return _pull_failure("local_credentials_changed")
    lease = await db.get(CredentialLease, account_id, populate_existing=True)
    if not lease or lease.token != lease_ticket or not lease.expires_at or as_utc(lease.expires_at) <= utcnow():
        await _rollback(db)
        return _pull_failure("lease_lost")
    binding = await db.get(CodexBinding, account_id, populate_existing=True)
    if binding is None or binding.state not in PULLABLE_STATES or binding.remote_account_id != remote_id:
        await _rollback(db)
        return _pull_failure("codex_binding_missing")
    revision = int(snapshot["credential_revision"] or 1) + 1
    now = utcnow()
    values = {"access_token_encrypted": encrypt_secret(access), "refresh_token_encrypted": None,
              "credential_revision": revision, "auth_state": "healthy", "updated_at": now}
    if id_token:
        values["id_token_encrypted"] = encrypt_secret(id_token)
    await db.execute(update(Account).where(Account.id == account_id).values(**values)
                     .execution_options(synchronize_session=False))
    # Keep the binding revision in step so a readback never marks the binding stale.
    await db.execute(update(CodexBinding).where(CodexBinding.account_id == account_id).values(
        last_pulled_at=now, credential_revision=revision, last_error=None).execution_options(synchronize_session=False))
    await db.commit()
    await db.get(Account, account_id, populate_existing=True)
    return {"ok": True, "success": True, "allow_oauth": False, "refreshed": True, "pulled": True,
            "owner": "codex_rs", "local_revision": revision,
            "message": "已从 codex-rs 读回访问令牌；续期由 codex-rs 负责"}


async def pull_access_token(db, account_id: int, lease_ticket: str) -> dict:
    """Read the latest AT / ID token back from codex-rs under an existing CredentialLease.

    Shape mirrors ``sub2api_refresh_authority.pull_owned_access_token``:
    success {"ok": True, "success": True, "allow_oauth": False, "refreshed": True,
             "pulled": True, "owner": "codex_rs", "message"}
    failure {"ok": False, "success": False, "allow_oauth": False, "error_code", "error"}
    Never raises; the RT from codex-rs is always discarded.
    """
    account_id = int(account_id)
    try:
        result = await _pull(db, account_id, lease_ticket)
    except Exception as exc:  # noqa: BLE001 - refresh callers need a result
        await _rollback(db)
        logger.warning("codex-rs 读回访问令牌异常 account_id=%s error=%s", account_id, type(exc).__name__)
        result = _pull_failure("remote_unavailable")
    if not result["ok"] and result["error_code"] not in {"remote_refresh_pending", "codex_binding_missing"}:
        try:
            await db.execute(update(CodexBinding).where(CodexBinding.account_id == account_id)
                             .values(last_error=result["error_code"]).execution_options(synchronize_session=False))
            await db.commit()
        except Exception:  # noqa: BLE001
            await _rollback(db)
    return result


async def _record_binding(db, account_id: int, remote_id: str, **values) -> None:
    await db.execute(update(CodexBinding).where(
        CodexBinding.account_id == account_id, CodexBinding.remote_account_id == remote_id,
    ).values(**values).execution_options(synchronize_session=False))
    await db.commit()


async def disable_on_departure(db, account_id: int, official_workspace_id: str | None) -> dict:
    """Disable (never delete) the codex-rs account when it leaves ``official_workspace_id``.

    Returns {"ok": bool, "skipped": bool, "error_code"?, "message"}. Never raises.
    """
    account_id = int(account_id)
    try:
        binding = await db.get(CodexBinding, account_id, populate_existing=True)
        if binding is None or not binding.remote_account_id:
            return {"ok": True, "skipped": True, "message": "未导入 codex-rs，无需停用"}
        if binding.official_workspace_id != official_workspace_id:
            return {"ok": True, "skipped": True, "message": "该号导入 codex-rs 时属于其他团队，未停用"}
        remote_id = binding.remote_account_id
        try:
            client = await _client(db)
            await db.commit()  # end the read transaction before network I/O
            await client.set_enabled(remote_id, False)
            enabled = (await client.detail(remote_id)).get("enabled")
        except CodexTransferError as exc:
            await _record_binding(db, account_id, remote_id, last_error=exc.code[:80])
            return {"ok": False, "skipped": False, "error_code": exc.code, "message": f"codex-rs 停用失败：{exc}"}
        if enabled is not False:
            await _record_binding(db, account_id, remote_id, remote_enabled=enabled if isinstance(enabled, bool) else None,
                                  last_error="codex_rs_disable_unconfirmed")
            return {"ok": False, "skipped": False, "error_code": "codex_rs_disable_unconfirmed",
                    "message": "codex-rs 未确认停用，请到 codex-rs 后台核对"}
        await _record_binding(db, account_id, remote_id, remote_enabled=False, last_error=None)
        return {"ok": True, "skipped": False, "message": "已在 codex-rs 停用该号"}
    except Exception as exc:  # noqa: BLE001 - departure flows must not fail on codex-rs
        await _rollback(db)
        logger.warning("codex-rs 停用异常 account_id=%s error=%s", account_id, type(exc).__name__)
        return {"ok": False, "skipped": False, "error_code": "codex_rs_disable_failed",
                "message": "codex-rs 停用失败，请到 codex-rs 后台手动停用"}


async def options(db) -> dict:
    """Selectors for the settings page.

    Returns {"groups": [{"id", "name", "enabled"}], "proxies": {"available": int, "total": int},
             "errors": {key: message}}. Proxy addresses are never returned.
    """
    result = {"groups": [], "proxies": {"available": 0, "total": 0}, "errors": {}}
    try:
        client = await _client(db)
    except CodexTransferError as exc:
        result["errors"]["connection"] = NOT_CONFIGURED if exc.code == "codex_not_configured" else str(exc)
        return result
    except Exception:  # noqa: BLE001
        result["errors"]["connection"] = NOT_CONFIGURED
        return result
    try:
        groups = await client.list_groups()
        result["groups"] = [{"id": item["id"], "name": str(item.get("name") or item["id"]),
                             "enabled": item.get("enabled") is not False}
                            for item in groups if isinstance(item.get("id"), str) and item["id"]]
    except CodexTransferError as exc:
        result["errors"]["groups"] = f"分组读取失败：{exc}"
    except Exception:  # noqa: BLE001
        result["errors"]["groups"] = "分组读取失败"
    try:
        proxies = await client.list_proxies()
        available = sum(1 for item in proxies if isinstance(item.get("lastTest"), dict)
                        and item["lastTest"].get("success") is True)
        result["proxies"] = {"available": available, "total": len(proxies)}
    except CodexTransferError as exc:
        result["errors"]["proxies"] = f"代理读取失败：{exc}"
    except Exception:  # noqa: BLE001
        result["errors"]["proxies"] = "代理读取失败"
    return result


async def probe(db, connections: dict | None) -> dict:
    """Test codex-rs reachability and the admin key; unsaved values in ``connections`` win.

    Returns {"ok": bool, "message": str, "checked_at": iso}.
    """
    checked_at = utcnow().isoformat()

    def failed(message: str) -> dict:
        return {"ok": False, "message": message, "error": message, "checked_at": checked_at}

    data = connections or {}
    try:
        stored_url = await get_setting_value(db, "codex_base_url", "") or ""
        stored_key = decrypt_secret(await get_setting_value(db, "codex_admin_key_encrypted", ""))
        typed_url = str(data.get("codex_base_url") or "").strip()
        typed_key = str(data.get("codex_admin_key") or "").strip()
        try:
            base_url = normalize_url(typed_url or stored_url) if (typed_url or stored_url) else ""
            saved_url = normalize_url(stored_url) if stored_url else ""
        except ValueError as exc:
            return failed(str(exc))
        if typed_key and typed_key != SECRET_MASK:
            api_key = typed_key
        elif base_url and base_url != saved_url:
            # Never send the saved key to an address it was not saved for.
            return failed("更改地址后请重新输入管理员 API Key 再检测")
        else:
            api_key = stored_key
        if not base_url or not api_key:
            return failed(NOT_CONFIGURED)
        client = CodexClient(base_url, api_key)
        await client.health()
        groups = await client.list_groups()
    except CodexTransferError as exc:
        return failed(str(exc))
    except Exception:  # noqa: BLE001
        return failed("codex-rs 检测失败")
    return {"ok": True, "message": f"连接正常，管理员 API Key 有效；分组 {len(groups)} 个",
            "group_count": len(groups), "checked_at": checked_at}


async def binding_status(db) -> dict:
    rows = (await db.execute(select(CodexBinding, Account.email, Account.credential_revision)
                            .join(Account, Account.id == CodexBinding.account_id))).all()
    return {"items": [{"account_id": row.account_id, "email": email, "target_url": row.target_url,
                       "remote_account_id": row.remote_account_id, "state": row.state,
                       "import_attempted": bool(row.import_attempted),
                       "credential_revision": row.credential_revision,
                       "stale": row.credential_revision is not None and row.credential_revision != revision,
                       "remote_enabled": row.remote_enabled, "official_workspace_id": row.official_workspace_id,
                       "last_error": row.last_error, "synced_at": isoformat(row.synced_at),
                       "last_pulled_at": isoformat(row.last_pulled_at)}
                      for row, email, revision in rows]}
