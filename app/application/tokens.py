"""Token refresh / auth probe. 401 never means ban; refresh first, OAuth last."""

from __future__ import annotations

import logging
import asyncio
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update, or_
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import automation_gate
from app.application.quota import decrypt_access_token
from app.application.settings import as_bool, get_setting_value
from app.core.crypto import token_cipher
from app.core.jwt import jwt_parser
from app.core.time import utcnow
from app.domain.quota_health import retry_after
from app.persistence.models.quota import CredentialLease
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
        if any(payload.get(key) for key in ("access_token", "refresh_token", "id_token", "session_token")):
            account.credential_revision = int(account.credential_revision or 1) + 1
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
        self, db: AsyncSession, account: Account, *, client_id: str = "", now: datetime | None = None, schedule_checks: bool = True,
    ) -> dict[str, Any]:
        stamp = now or utcnow()
        account_id = account.id
        ticket = uuid.uuid4().hex
        await db.execute(insert(CredentialLease).values(account_id=account_id).on_conflict_do_nothing())
        # The first write serializes ownership handoff with local refresh acquisition.
        from app.persistence.models.sub2api import Sub2ApiRefreshAuthority
        from app.application.refresh_ownership import remote_refresh_owner
        remote_owner = await remote_refresh_owner(db, account_id)
        claimed = await db.execute(update(CredentialLease).execution_options(synchronize_session="fetch").where(
            CredentialLease.account_id == account_id,
            CredentialLease.token.is_(None),
            or_(CredentialLease.expires_at.is_(None), CredentialLease.expires_at <= stamp),
            or_(CredentialLease.next_attempt_at.is_(None), CredentialLease.next_attempt_at <= stamp),
        ).values(token=ticket, expires_at=stamp + timedelta(seconds=180)))
        await db.commit()
        if claimed.rowcount != 1:
            return {"success": False, "error_code": "refresh_deferred", "allow_oauth": False}
        release_lease = True
        try:
            await db.refresh(account)
            if remote_owner is not None:
                from app.application.sub2api_refresh_authority import pull_owned_access_token, failure
                try:
                    result = await asyncio.wait_for(pull_owned_access_token(db, account_id, ticket), timeout=90)
                except Exception:
                    await db.rollback()
                    result = failure("remote_unavailable")
                await db.refresh(account)
                if result.get("success") and schedule_checks:
                    from app.application.quota import quota_service
                    await quota_service.enqueue_after_credentials(db, account)
                if not result.get("success"):
                    await db.execute(update(CredentialLease).where(
                        CredentialLease.account_id == account_id, CredentialLease.token == ticket,
                    ).values(next_attempt_at=stamp + timedelta(minutes=5)))
                return result
            release_lease = False
            result = await self._refresh_claimed(db, account, client_id=client_id, now=now, schedule_checks=schedule_checks, lease_ticket=ticket)
            release_lease = bool(result.get("success")) or result.get("error_code") in {"credential_revision_conflict", "missing_refresh_token", "credential_error", "token_refresh_failed", "invalid_grant", "invalid_token", "token_revoked", "token_invalidated"}
            if not release_lease:
                result = {**result, "allow_oauth": False, "error_code": "refresh_outcome_unknown", "error": "刷新结果未确认，已阻止再次消费同一刷新凭据，请人工核对"}
            return result
        finally:
            if release_lease:
                await db.execute(update(CredentialLease).execution_options(synchronize_session="fetch").where(CredentialLease.account_id == account_id,
                    CredentialLease.token == ticket).values(token=None, expires_at=None))
            await db.commit()

    async def _refresh_claimed(
        self,
        db: AsyncSession,
        account: Account,
        *,
        client_id: str = "",
        now: datetime | None = None,
        schedule_checks: bool = True,
        lease_ticket: str | None = None,
    ) -> dict[str, Any]:
        stamp = now or utcnow()
        refresh = decrypt_secret(account.refresh_token_encrypted)
        cid = str(account.client_id or client_id or DEFAULT_OAUTH_CLIENT_ID).strip() or DEFAULT_OAUTH_CLIENT_ID
        if not refresh and account.refresh_token_encrypted:
            await db.execute(update(CredentialLease).execution_options(synchronize_session="fetch").where(
                CredentialLease.account_id == account.id).values(next_attempt_at=stamp + timedelta(minutes=15)))
            return {"success": False, "error_code": "credential_error", "allow_oauth": False, "error": "本地刷新凭证无法解密"}
        if not refresh:
            set_auth_state(account, "oauth_required")
            return {"success": False, "error_code": "missing_refresh_token", "error": "refresh token missing"}
        revision = int(account.credential_revision or 1)
        from app.application.sub2api_refresh_authority import _local
        original = _local(account)
        identifier = account.email or "default"
        await db.commit()
        try:
            result = await asyncio.wait_for(self.client.refresh_access_token(refresh, cid, db, identifier=identifier), timeout=90)
        except Exception:
            result = {"success": False, "error_code": "transport"}
        await db.commit()
        # Acquire a short write transaction before comparing and storing credentials.
        guard = await db.execute(update(Account).where(*[getattr(Account, k) == v for k, v in original.items()]).values(credential_revision=revision))
        if lease_ticket is not None:
            lease = await db.get(CredentialLease, original["id"], populate_existing=True)
            from app.core.time import as_utc
            if not lease or lease.token != lease_ticket or not lease.expires_at or as_utc(lease.expires_at) <= as_utc(utcnow()):
                await db.rollback()
                return {"success": False, "error_code": "lease_lost", "allow_oauth": False}
        if guard.rowcount != 1:
            await db.refresh(account)
            return {"success": False, "error_code": "credential_revision_conflict", "allow_oauth": False}
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
            await db.execute(update(CredentialLease).execution_options(synchronize_session="fetch").where(CredentialLease.account_id == account.id).values(next_attempt_at=None))
            account.updated_at = stamp
            await db.commit()
            from app.application.quota import quota_service
            if schedule_checks:
                await quota_service.enqueue_after_credentials(db, account)
            return {"success": True, "error_code": "", "refreshed": True}
        code = str(result.get("error_code") or "token_refresh_failed")
        status = result.get("status_code")
        rejected = status == 401 or code in {"invalid_grant", "invalid_token", "token_revoked", "token_invalidated"}
        if not rejected:
            retry_at = retry_after(result.get("retry_after"), stamp) or stamp + timedelta(minutes=15)
            await db.execute(update(CredentialLease).execution_options(synchronize_session="fetch").where(CredentialLease.account_id == account.id).values(next_attempt_at=retry_at))
            return {"success": False, "error_code": code, "error": "令牌刷新暂时失败，等待重试", "allow_oauth": False}
        set_auth_state(account, "oauth_required")
        if http_401_is_not_ban(code, status):
            code = "token_refresh_failed"
        return {
            "success": False,
            "error_code": code,
            "error": "当前刷新凭证被拒绝",
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
            from app.persistence.models.sub2api import Sub2ApiRefreshAuthority
            from app.application.refresh_ownership import remote_refresh_owner
            remote_owned = await remote_refresh_owner(db, account.id) if account.auth_state == "oauth_required" else None
            if str(account.auth_state or "") in {"deactivated", "manual_required", "oauth_required", "phone_required"} and remote_owned is None:
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
            lease = await db.get(CredentialLease, account.id)
            from app.core.time import as_utc
            if lease and lease.next_attempt_at and as_utc(lease.next_attempt_at) > as_utc(stamp):
                stats["skipped"] += 1
                continue
            result = await self.refresh_account(db, account, client_id=str(cfg.get("client_id") or ""), now=stamp)
            lease = await db.get(CredentialLease, account.id)
            if lease and not lease.next_attempt_at:
                lease.next_attempt_at = stamp + timedelta(minutes=int(cfg["interval_minutes"]))
            if result.get("success"):
                stats["refreshed"] += 1
            elif result.get("error_code") in HTTP_401_CODES | {"token_refresh_failed", "missing_refresh_token"}:
                stats["oauth_required"] += 1
            else:
                stats["failed"] += 1
        await db.commit()
        return stats


auth_service = AuthService()
