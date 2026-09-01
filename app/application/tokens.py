"""Token refresh / auth probe. 401 never means ban; refresh first, OAuth last."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import automation_gate
from app.application.quota import decrypt_access_token
from app.application.settings import as_bool, get_setting_value
from app.core.crypto import token_cipher
from app.core.jwt import jwt_parser
from app.core.time import utcnow
from app.domain.automation import (
    DEFAULT_AUTH_PROBE_ENABLED,
    DEFAULT_AUTH_PROBE_INTERVAL_MINUTES,
    DEFAULT_OAUTH_CLIENT_ID,
    TOKEN_REFRESH_WINDOW_HOURS,
)
from app.domain.identity import AUTH_STATES
from app.domain.quota import SKIP_OPERATIONAL_STATES
from app.domain.reauth import HTTP_401_CODES, http_401_is_not_ban, owner_refresh_allows_oauth
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.models.identity import Account

logger = logging.getLogger(__name__)


def decrypt_secret(raw: str | None) -> str:
    if not raw:
        return ""
    try:
        return token_cipher().decrypt(raw) or ""
    except Exception:
        return ""


def encrypt_secret(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return token_cipher().encrypt(text)


def set_auth_state(account: Account, state: str) -> None:
    if state in AUTH_STATES:
        account.auth_state = state


class AuthService:
    def __init__(self, client=None):
        self.client = client or chatgpt_client

    async def load_settings(self, db: AsyncSession) -> dict[str, Any]:
        enabled_raw = await get_setting_value(db, "auth_probe_enabled", str(bool(DEFAULT_AUTH_PROBE_ENABLED)).lower())
        interval_raw = await get_setting_value(
            db, "token_refresh_interval_minutes", str(DEFAULT_AUTH_PROBE_INTERVAL_MINUTES)
        )
        window_raw = await get_setting_value(db, "token_refresh_window_hours", str(TOKEN_REFRESH_WINDOW_HOURS))
        client_id = await get_setting_value(db, "token_refresh_client_id", DEFAULT_OAUTH_CLIENT_ID)
        try:
            interval = max(5, min(24 * 60, int(interval_raw or DEFAULT_AUTH_PROBE_INTERVAL_MINUTES)))
        except (TypeError, ValueError):
            interval = DEFAULT_AUTH_PROBE_INTERVAL_MINUTES
        try:
            window = max(1, min(24, int(window_raw or TOKEN_REFRESH_WINDOW_HOURS)))
        except (TypeError, ValueError):
            window = TOKEN_REFRESH_WINDOW_HOURS
        return {
            "enabled": as_bool(enabled_raw, DEFAULT_AUTH_PROBE_ENABLED),
            "interval_minutes": interval,
            "window_hours": window,
            "client_id": str(client_id or DEFAULT_OAUTH_CLIENT_ID),
        }

    def access_token_due(self, account: Account, *, now: datetime, window_hours: int) -> bool:
        token = decrypt_access_token(account)
        if not token:
            return True
        remaining = jwt_parser.remaining_seconds(token, now=now)
        if remaining is None:
            return True
        return remaining <= window_hours * 3600

    async def apply_tokens(self, account: Account, payload: dict[str, Any]) -> None:
        if payload.get("access_token"):
            account.access_token_encrypted = encrypt_secret(payload.get("access_token"))
        if payload.get("refresh_token"):
            account.refresh_token_encrypted = encrypt_secret(payload.get("refresh_token"))
        if payload.get("id_token"):
            account.id_token_encrypted = encrypt_secret(payload.get("id_token"))
        if payload.get("session_token"):
            account.session_token_encrypted = encrypt_secret(payload.get("session_token"))
        if payload.get("client_id"):
            account.client_id = str(payload.get("client_id"))
        set_auth_state(account, "healthy")

    async def refresh_account(
        self,
        db: AsyncSession,
        account: Account,
        *,
        client_id: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        stamp = now or utcnow()
        refresh = decrypt_secret(account.refresh_token_encrypted)
        cid = str(account.client_id or client_id or DEFAULT_OAUTH_CLIENT_ID).strip() or DEFAULT_OAUTH_CLIENT_ID
        if not refresh:
            set_auth_state(account, "oauth_required")
            return {"success": False, "error_code": "missing_refresh_token", "error": "refresh token missing"}
        set_auth_state(account, "refreshing")
        result = await self.client.refresh_access_token(refresh, cid, db, identifier=account.email or "default")
        if result.get("success") and result.get("access_token"):
            new_email = jwt_parser.extract_email(str(result.get("access_token") or ""))
            if new_email and new_email != str(account.email or "").strip().lower():
                set_auth_state(account, "manual_required")
                return {
                    "success": False,
                    "error_code": "token_identity_mismatch",
                    "error": f"refreshed token email {new_email} != {account.email}",
                }
            await self.apply_tokens(account, result)
            account.updated_at = stamp
            return {"success": True, "error_code": "", "refreshed": True}
        code = str(result.get("error_code") or "token_refresh_failed")
        if http_401_is_not_ban(code, result.get("status_code")):
            code = "token_refresh_failed"
        set_auth_state(account, "oauth_required")
        return {
            "success": False,
            "error_code": code,
            "error": str(result.get("error") or "token refresh failed"),
            "allow_oauth": owner_refresh_allows_oauth(code),
        }

    async def run_probe_once(
        self,
        db: AsyncSession,
        *,
        now: datetime | None = None,
        settings: dict[str, Any] | None = None,
        force: bool = False,
        limit: int = 3,
    ) -> dict[str, Any]:
        cfg = settings or await self.load_settings(db)
        stamp = now or utcnow()
        stats = {
            "enabled": bool(cfg.get("enabled")),
            "scanned": 0,
            "refreshed": 0,
            "oauth_required": 0,
            "skipped": 0,
            "failed": 0,
        }
        if not cfg.get("enabled") and not force:
            stats["skipped"] = 1
            return stats
        accounts = list((await db.execute(select(Account))).scalars())
        due: list[Account] = []
        for account in accounts:
            stats["scanned"] += 1
            if str(account.operational_state or "") in SKIP_OPERATIONAL_STATES:
                stats["skipped"] += 1
                continue
            if str(account.auth_state or "") in {"deactivated", "manual_required"}:
                stats["skipped"] += 1
                continue
            if not self.access_token_due(account, now=stamp, window_hours=int(cfg["window_hours"])):
                continue
            due.append(account)
        for account in due[: max(1, int(limit))]:
            gate = await automation_gate(db, email=account.email)
            if gate.get("error_code") == "identity_conflict":
                set_auth_state(account, "manual_required")
                stats["failed"] += 1
                continue
            result = await self.refresh_account(db, account, client_id=str(cfg.get("client_id") or ""), now=stamp)
            if result.get("success"):
                stats["refreshed"] += 1
            elif result.get("error_code") in HTTP_401_CODES | {"token_refresh_failed", "missing_refresh_token"}:
                stats["oauth_required"] += 1
            else:
                stats["failed"] += 1
        await db.commit()
        return stats


auth_service = AuthService()
