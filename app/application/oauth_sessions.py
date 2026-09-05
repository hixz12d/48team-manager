"""Persistent OAuth session lifecycle and strict callback validation."""

from __future__ import annotations

import hashlib
import hmac
from typing import Any
from urllib.parse import parse_qsl, urlparse

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crypto import token_cipher
from app.core.time import as_utc, utcnow
from app.domain.identity.ids import normalize_email
from app.persistence.models.oauth import OAuthSession


class OAuthSessionError(ValueError):
    def __init__(self, message: str, *, error_code: str = "callback_invalid"):
        super().__init__(message)
        self.error_code = error_code


def _state_hash(state: str) -> str:
    return hashlib.sha256(str(state or "").encode("utf-8")).hexdigest()


def parse_strict_callback(callback_url: str, *, redirect_uri: str, expected_state_hash: str) -> dict[str, str]:
    text = str(callback_url or "").strip()
    if "://" not in text:
        raise OAuthSessionError("请粘贴完整的 OAuth 回调地址")
    callback = urlparse(text)
    expected = urlparse(redirect_uri)
    if callback.username is not None or callback.password is not None:
        raise OAuthSessionError("OAuth 回调地址不能包含用户信息")
    callback_port = callback.port or (443 if callback.scheme == "https" else 80)
    expected_port = expected.port or (443 if expected.scheme == "https" else 80)
    if (
        callback.scheme != expected.scheme
        or (callback.hostname or "").lower() != (expected.hostname or "").lower()
        or callback_port != expected_port
        or callback.path != expected.path
    ):
        raise OAuthSessionError("OAuth 回调地址与本次授权会话不匹配")
    values: dict[str, list[str]] = {}
    for key, value in [*parse_qsl(callback.query, keep_blank_values=True), *parse_qsl(callback.fragment, keep_blank_values=True)]:
        values.setdefault(key, []).append(value)
    for key in ("code", "state", "error", "error_description"):
        if len(values.get(key, [])) > 1:
            raise OAuthSessionError(f"OAuth 回调包含重复的 {key} 参数")
    if values.get("error"):
        raise OAuthSessionError(values.get("error_description", values["error"])[0])
    code = values.get("code", [""])[0]
    state = values.get("state", [""])[0]
    if not code:
        raise OAuthSessionError("OAuth 回调缺少授权码")
    if not state:
        raise OAuthSessionError("OAuth 回调缺少 state")
    if not hmac.compare_digest(_state_hash(state), expected_state_hash):
        raise OAuthSessionError("OAuth 回调 state 不匹配")
    return {"code": code, "state": state}


class OAuthSessionStore:
    async def persist(
        self,
        db: AsyncSession,
        memory_session: dict[str, Any],
        *,
        purpose: str,
        account_id: int | None = None,
        workspace_id: int | None = None,
        credential_revision: int | None = None,
    ) -> OAuthSession:
        cipher = token_cipher()
        state = str(memory_session.get("state") or "")
        verifier = str(memory_session.get("code_verifier") or "")
        if not state or not verifier:
            raise OAuthSessionError("OAuth 生成器未返回 state 或 PKCE verifier", error_code="oauth_session_invalid")
        proxy = str(memory_session.get("proxy") or "")
        row = OAuthSession(
            public_id=str(memory_session["ticket"]),
            purpose=purpose,
            mode=str(memory_session.get("mode") or "manual"),
            operation_id=str(memory_session.get("job_id") or "") or None,
            account_id=account_id,
            workspace_id=workspace_id,
            email=normalize_email(memory_session.get("email")),
            state_hash=_state_hash(state),
            code_verifier_encrypted=cipher.encrypt(verifier),
            client_id=str(memory_session.get("client_id") or ""),
            redirect_uri=str(memory_session.get("redirect_uri") or ""),
            authorize_url=str(memory_session.get("authorize_url") or ""),
            proxy_snapshot_encrypted=cipher.encrypt(proxy) if proxy else None,
            proxy_source=str(memory_session.get("proxy_source") or "") or None,
            sub2api_proxy_id=memory_session.get("sub2api_proxy_id"),
            proxy_instance_key=str(memory_session.get("proxy_instance_key") or "") or None,
            credential_revision=credential_revision,
            status="waiting",
            expires_at=memory_session["expires_at"],
        )
        db.add(row)
        await db.flush()
        return row

    async def runtime_session(self, db: AsyncSession, ticket: str) -> dict[str, Any] | None:
        row = await db.scalar(select(OAuthSession).where(OAuthSession.public_id == ticket))
        if row is None or as_utc(row.expires_at) <= utcnow() or row.status not in {"waiting", "exchanging"}:
            return None
        context = self.exchange_context(row)
        return {
            "ticket": row.public_id,
            "email": row.email,
            "authorize_url": row.authorize_url,
            "client_id": row.client_id,
            "redirect_uri": row.redirect_uri,
            "code_verifier": context["code_verifier"],
            "proxy": context["proxy"],
            "mode": row.mode,
            "job_id": row.operation_id or "",
            "account_id": row.account_id,
        }

    async def begin_exchange(
        self,
        db: AsyncSession,
        ticket: str,
        callback_url: str,
        *,
        account_id: int | None = None,
        purpose: str | None = None,
    ) -> tuple[OAuthSession, dict[str, str]]:
        row = await db.scalar(select(OAuthSession).where(OAuthSession.public_id == ticket))
        if row is None or as_utc(row.expires_at) <= utcnow():
            raise OAuthSessionError("授权回调已过期，请重新生成授权链接", error_code="callback_expired")
        if purpose and row.purpose != purpose:
            raise OAuthSessionError("授权会话用途不匹配", error_code="oauth_purpose_mismatch")
        if account_id is not None and row.account_id != account_id:
            raise OAuthSessionError("授权会话不属于这个账号", error_code="oauth_account_mismatch")
        parsed = parse_strict_callback(
            callback_url,
            redirect_uri=row.redirect_uri,
            expected_state_hash=row.state_hash,
        )
        claimed = await db.execute(
            update(OAuthSession)
            .where(OAuthSession.id == row.id, OAuthSession.status == "waiting")
            .values(status="exchanging")
        )
        if claimed.rowcount != 1:
            raise OAuthSessionError("授权回调已提交，不能重复使用", error_code="callback_consumed")
        await db.commit()
        row.status = "exchanging"
        return row, parsed

    def exchange_context(self, row: OAuthSession) -> dict[str, str]:
        cipher = token_cipher()
        return {
            "client_id": row.client_id,
            "redirect_uri": row.redirect_uri,
            "code_verifier": cipher.decrypt(row.code_verifier_encrypted),
            "proxy": cipher.decrypt(row.proxy_snapshot_encrypted) if row.proxy_snapshot_encrypted else "",
        }

    async def finish(self, db: AsyncSession, row: OAuthSession, *, success: bool) -> None:
        row.status = "consumed" if success else "failed"
        row.consumed_at = utcnow()
        await db.flush()


oauth_session_store = OAuthSessionStore()
