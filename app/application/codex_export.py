"""Codex access-token exports. Refresh tokens never leave Team48."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.tokens import decrypt_secret
from app.core.jwt import jwt_parser
from app.core.time import utcnow
from app.domain.automation import DEFAULT_OAUTH_CLIENT_ID
from app.persistence.models.identity import Account


class CodexTransferError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code = code
        self.status = status


def account_document(account: Account) -> dict:
    if account.operational_state in {"archived", "disabled"} or account.local_purpose == "disabled":
        raise CodexTransferError("account_disabled", "账号已归档或停用")
    if account.auth_state in {"deactivated", "manual_required", "oauth_required", "phone_required"}:
        raise CodexTransferError("auth_required", "账号需要重新确认授权")
    access = decrypt_secret(account.access_token_encrypted)
    if not access:
        raise CodexTransferError("access_token_missing", "缺少可解密的 AT，请先完成授权或刷新")
    claims = jwt_parser.decode_token(access)
    if not isinstance(claims, dict):
        raise CodexTransferError("invalid_access_token", "AT 格式无效")
    try:
        remaining = jwt_parser.remaining_seconds(access)
    except (OverflowError, TypeError, ValueError):
        remaining = None
    if remaining is None or remaining <= 60:
        raise CodexTransferError("access_token_expired", "AT 已过期或即将到期，请先刷新后重试")
    client_ids = [value for value in (account.client_id, claims.get("client_id")) if value]
    if not client_ids or any(value != DEFAULT_OAUTH_CLIENT_ID for value in client_ids):
        raise CodexTransferError("client_id_mismatch", "凭据不是预期的 Codex OAuth client")
    auth = claims.get("https://api.openai.com/auth")
    profile = claims.get("https://api.openai.com/profile") or {}
    if not isinstance(auth, dict) or not isinstance(profile, dict):
        raise CodexTransferError("identity_missing", "AT 缺少 Codex 身份信息")
    workspace = auth.get("chatgpt_account_id")
    user = auth.get("chatgpt_user_id") or auth.get("user_id")
    email = str(profile.get("email") or claims.get("email") or "").strip().lower()
    if not isinstance(workspace, str) or not workspace or not isinstance(user, str) or not user or not email:
        raise CodexTransferError("identity_missing", "AT 缺少邮箱、用户或工作区身份")
    if (email != account.email.strip().lower()
            or account.official_account_id and account.official_account_id != workspace
            or account.official_user_id and account.official_user_id != user):
        raise CodexTransferError("identity_mismatch", "AT 身份与本地账号不一致，未导出")
    result = {"provider": "openai", "name": account.email, "email": email, "access_token": access}
    id_token = decrypt_secret(account.id_token_encrypted)
    if account.id_token_encrypted and not id_token:
        raise CodexTransferError("credential_error", "ID token 无法解密")
    if id_token:
        id_claims = jwt_parser.decode_token(id_token)
        if not isinstance(id_claims, dict):
            raise CodexTransferError("invalid_id_token", "ID token 格式无效")
        id_auth = id_claims.get("https://api.openai.com/auth") or {}
        if not isinstance(id_auth, dict):
            raise CodexTransferError("invalid_id_token", "ID token 身份格式无效")
        if (id_claims.get("email") and str(id_claims["email"]).strip().lower() != email
                or id_auth.get("chatgpt_account_id") and id_auth["chatgpt_account_id"] != workspace
                or (id_auth.get("chatgpt_user_id") or id_auth.get("user_id")) not in (None, user)):
            raise CodexTransferError("identity_mismatch", "ID token 与 AT 身份不一致")
        result["id_token"] = id_token
    return result


async def export_document(db: AsyncSession, account_ids: list[int]) -> dict:
    ids = list(dict.fromkeys(account_ids))
    accounts = {a.id: a for a in await db.scalars(select(Account).where(Account.id.in_(ids)))}
    documents = []
    for account_id in ids:
        account = accounts.get(account_id)
        if account is None:
            raise CodexTransferError("not_found", "所选账号不存在", 404)
        try:
            documents.append(account_document(account))
        except CodexTransferError as exc:
            raise CodexTransferError(exc.code, f"{account.email}: {exc}", exc.status) from None
    return {"sourceFormat": "team48-at-only", "exportedAt": utcnow().isoformat(), "accounts": documents}
