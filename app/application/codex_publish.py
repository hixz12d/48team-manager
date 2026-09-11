"""Explicit AT-only Codex push with durable intent and identity checks."""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.sqlite import insert

from app.application.codex_export import CodexTransferError, account_document
from app.core.jwt import jwt_parser
from app.core.time import utcnow
from app.integrations.codex.client import CodexClient, load_config
from app.persistence.models.codex import CodexBinding
from app.persistence.models.identity import Account


def verify_remote(remote: dict, document: dict, *, check_expiration=False):
    claims = jwt_parser.extract_auth_claim(document["access_token"])
    if (remote.get("provider") != "openai"
            or str(remote.get("email") or "").strip().lower() != document["email"]
            or remote.get("accountId") != claims.get("chatgpt_account_id")
            or remote.get("userId") != (claims.get("chatgpt_user_id") or claims.get("user_id"))):
        raise CodexTransferError("remote_identity_mismatch", "远端身份与当前 AT 不一致，未覆盖")
    if remote.get("hasRefreshToken") is not False:
        raise CodexTransferError("remote_refresh_owner", "远端持有 RT 或刷新归属不明，禁止覆盖")
    if check_expiration:
        try:
            expiration = datetime.fromisoformat(str(remote.get("accessTokenExpiresAt") or "").replace("Z", "+00:00"))
            matches = expiration == jwt_parser.expiration_utc(document["access_token"])
        except (ValueError, TypeError):
            matches = False
        if not matches:
            raise CodexTransferError("remote_readback_mismatch", "远端凭据到期时间未通过回读核对")


async def push_one(db, account_id: int, client: CodexClient) -> dict:
    # Serialize against deletion and persist the intent before any external write.
    await db.execute(update(Account).where(Account.id == account_id).values(version=Account.version))
    account = await db.get(Account, account_id, populate_existing=True)
    if account is None:
        await db.rollback()
        raise CodexTransferError("not_found", "账号不存在", 404)
    try:
        document = account_document(account)
    except CodexTransferError:
        await db.rollback()
        raise
    revision = account.credential_revision
    await db.execute(insert(CodexBinding).values(
        account_id=account_id, target_url=client.base_url,
        remote_name=f"team48:{uuid.uuid4().hex}:{account.email}"[:255], state="pending",
        import_attempted=False).on_conflict_do_nothing(index_elements=["account_id"]))
    binding = await db.get(CodexBinding, account_id, populate_existing=True)
    if binding.target_url != client.base_url:
        await db.rollback()
        raise CodexTransferError("target_changed", "账号已绑定其他 Codex 地址，未向新地址发送凭据")
    ticket = uuid.uuid4().hex
    claim = await db.execute(update(CodexBinding).execution_options(synchronize_session="fetch").where(
        CodexBinding.account_id == account_id,
        or_(CodexBinding.lease_until.is_(None), CodexBinding.lease_until <= utcnow()),
    ).values(lease_token=ticket, lease_until=utcnow() + timedelta(seconds=180), state="pushing"))
    if claim.rowcount != 1:
        await db.rollback()
        raise CodexTransferError("push_busy", "该账号已有推送执行中")
    await db.commit()
    await db.refresh(binding)
    try:
        async with asyncio.timeout(60):
            remote_id = binding.remote_account_id
            created = False
            if not remote_id:
                matches = await client.find_accounts(document["email"])
                owned = [item for item in matches if item.get("name") == binding.remote_name]
                if len(owned) > 1:
                    raise CodexTransferError("remote_duplicate", "远端存在多个同名绑定，需人工核对")
                if owned:
                    verify_remote(owned[0], document)
                    remote_id = owned[0].get("id")
                    if not isinstance(remote_id, str) or not remote_id.startswith("acct_"):
                        raise CodexTransferError("remote_identity_mismatch", "远端账号 ID 无效")
                elif binding.import_attempted:
                    raise CodexTransferError("import_uncertain", "上次导入结果不明且未找到远端账号；禁止重复创建，请人工核对")
                else:
                    if any(str(item.get("email") or "").strip().lower() == document["email"] for item in matches):
                        raise CodexTransferError("remote_exists", "远端已有同邮箱账号，未自动接管或重复创建")
                    binding.import_attempted = True
                    await db.commit()
                    remote_id = await client.create({**document, "name": binding.remote_name})
                    created = True
                binding.remote_account_id = remote_id
                await db.commit()
            remote = await client.detail(remote_id)
            verify_remote(remote, document)
            if not created:
                await client.rotate(remote_id, document)
                remote = await client.detail(remote_id)
            verify_remote(remote, document, check_expiration=True)
            await db.refresh(account)
            if account.credential_revision != revision:
                raise CodexTransferError("local_credentials_changed", "推送期间本地凭据已更新，请再次推送最新版本")
            binding.state = "synced"
            binding.credential_revision = revision
            binding.synced_at = utcnow()
            binding.last_error = None
            await db.commit()
            return {"account_id": account_id, "remote_account_id": remote_id,
                    "ok": True, "state": "synced", "credential_revision": revision}
    except (CodexTransferError, TimeoutError) as exc:
        error = exc if isinstance(exc, CodexTransferError) else CodexTransferError("push_timeout", "推送超时，保留绑定，未自动重试", 504)
        binding.state = "uncertain"
        binding.last_error = error.code
        await db.commit()
        raise error from None
    finally:
        await db.execute(update(CodexBinding).where(
            CodexBinding.account_id == account_id, CodexBinding.lease_token == ticket,
        ).values(lease_token=None, lease_until=None))
        await db.commit()


async def push_accounts(db, account_ids: list[int], *, client=None, expected_target: str | None = None) -> dict:
    if client is None:
        config = await load_config(db)
        client = CodexClient(config["base_url"], config["api_key"])
    if expected_target is not None and expected_target != client.base_url:
        raise CodexTransferError("target_changed", "Codex 目标已改变，请重新确认", 409)
    results = []
    for account_id in dict.fromkeys(account_ids):
        try:
            results.append(await push_one(db, account_id, client))
        except CodexTransferError as exc:
            await db.rollback()
            results.append({"account_id": account_id, "ok": False, "error_code": exc.code, "message": str(exc)})
    return {"ok": all(item["ok"] for item in results), "results": results,
            "synced": sum(item["ok"] for item in results)}


async def binding_status(db) -> dict:
    rows = (await db.execute(select(CodexBinding, Account.email, Account.credential_revision)
                            .join(Account, Account.id == CodexBinding.account_id))).all()
    return {"items": [{"account_id": row.account_id, "email": email, "target_url": row.target_url,
                       "remote_account_id": row.remote_account_id, "state": row.state,
                       "credential_revision": row.credential_revision,
                       "stale": row.credential_revision != revision, "last_error": row.last_error,
                       "synced_at": row.synced_at.isoformat() if row.synced_at else None}
                      for row, email, revision in rows]}
