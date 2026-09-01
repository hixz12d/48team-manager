"""ChatGPT official HTTP. Quota uses /backend-api/wham/usage only."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import random
import secrets
import uuid
from typing import Any
from urllib.parse import urlencode, urlparse

from curl_cffi.requests import AsyncSession
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession as DBAsyncSession

from app.core.proxy import build_curl_cffi_proxies
from app.persistence.models.identity import Account

logger = logging.getLogger(__name__)


class ChatGPTClient:
    BASE_URL = "https://chatgpt.com/backend-api"
    OAI_CLIENT_VERSION = "prod-eddc2f6ff65fee2d0d6439e379eab94fe3047f72"
    IMPERSONATE = "chrome136"
    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 2, 4]
    TRANSIENT_ERROR_MARKERS = (
        "timed out",
        "timeout",
        "ssl_connect",
        "ssl_error",
        "connection reset",
        "connection closed",
        "recv failure",
        "failed to perform",
        "curl: (28)",
        "curl: (35)",
        "curl: (56)",
        "proxy",
    )

    @staticmethod
    def is_already_removed_error(
        status_code: int | None = None,
        error: Any = None,
        error_code: Any = None,
    ) -> bool:
        if int(status_code or 0) == 404:
            return True
        text = f"{error_code or ''} {error or ''}".lower()
        markers = (
            "not found",
            "does not exist",
            "already removed",
            "isn't a member",
            "is not a member",
            "no longer a member",
            "not a member of",
            "user_not_found",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def pick_user_id(*candidates: Any) -> str | None:
        from app.domain.vacancy import pick_chatgpt_user_id

        return pick_chatgpt_user_id(*candidates)

    def __init__(self):
        self._sessions: dict[str, AsyncSession] = {}
        self._device_ids: dict[str, str] = {}

    async def _proxy_for(self, db_session: DBAsyncSession | None, identifier: str) -> str | None:
        if db_session is None or not identifier or identifier == "default":
            return None
        if not str(identifier).startswith("acc_"):
            account = (
                await db_session.execute(select(Account).where(Account.email == identifier))
            ).scalar_one_or_none()
            if account and account.proxy:
                return account.proxy
            return None
        account_id = str(identifier)[4:]
        account = (
            await db_session.execute(select(Account).where(Account.official_account_id == account_id))
        ).scalar_one_or_none()
        if account and account.proxy:
            return account.proxy
        return None

    async def _create_session(self, db_session: DBAsyncSession | None, identifier: str) -> AsyncSession:
        proxy = await self._proxy_for(db_session, identifier)
        proxies = build_curl_cffi_proxies(proxy)
        if proxies:
            parsed = urlparse(proxies["all"])
            logger.info(
                "chatgpt session proxy scheme=%s host=%s port=%s",
                parsed.scheme,
                parsed.hostname,
                parsed.port,
            )
        return AsyncSession(impersonate=self.IMPERSONATE, proxies=proxies, timeout=30, verify=False)

    def _device_id_for(self, identifier: str) -> str:
        if identifier not in self._device_ids:
            self._device_ids[identifier] = str(uuid.uuid4())
        return self._device_ids[identifier]

    async def _get_session(self, db_session: DBAsyncSession | None, identifier: str) -> AsyncSession:
        if identifier not in self._sessions:
            self._sessions[identifier] = await self._create_session(db_session, identifier)
        return self._sessions[identifier]

    async def clear_session(self, identifier: str) -> None:
        session = self._sessions.pop(identifier, None)
        if session is not None:
            try:
                await session.close()
            except Exception:
                logger.debug("chatgpt session close failed identifier=%s", identifier)

    async def _rebuild_session(self, db_session: DBAsyncSession | None, identifier: str) -> AsyncSession | None:
        await self.clear_session(identifier)
        if db_session is None:
            return None
        return await self._get_session(db_session, identifier)

    def _is_transient(self, error: Any) -> bool:
        message = str(error or "").lower()
        return any(marker in message for marker in self.TRANSIENT_ERROR_MARKERS)

    async def _make_request(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        db_session: DBAsyncSession | None = None,
        identifier: str = "default",
        json_data: dict[str, Any] | None = None,
        form_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if identifier == "default":
            acc_id = headers.get("chatgpt-account-id")
            identifier = f"acc_{acc_id}" if acc_id else identifier
        session = await self._get_session(db_session, identifier)
        request_headers = {
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://chatgpt.com/",
            "Origin": "https://chatgpt.com",
            "Connection": "keep-alive",
            "oai-language": "zh-CN",
            "oai-client-version": self.OAI_CLIENT_VERSION,
            "oai-device-id": self._device_id_for(identifier),
            **headers,
        }
        for attempt in range(self.MAX_RETRIES):
            try:
                if attempt > 0:
                    await asyncio.sleep(self.RETRY_DELAYS[attempt - 1] + random.uniform(0.5, 1.5))
                if method == "GET":
                    response = await session.get(url, headers=request_headers)
                elif method == "POST":
                    if form_data is not None:
                        response = await session.post(url, headers=request_headers, data=form_data)
                    else:
                        response = await session.post(url, headers=request_headers, json=json_data or {})
                elif method == "DELETE":
                    response = await session.delete(url, headers=request_headers, json=json_data)
                else:
                    raise ValueError(f"unsupported method {method}")
                status_code = response.status_code
                if 200 <= status_code < 300:
                    try:
                        data = response.json()
                    except Exception:
                        data = {}
                    return {"success": True, "status_code": status_code, "data": data, "error": None}
                error_msg = response.text
                error_code = None
                try:
                    error_data = response.json()
                    detail = error_data.get("detail", error_msg)
                    error_msg = str(detail) if not isinstance(detail, str) else detail
                    if isinstance(error_data, dict):
                        error_info = error_data.get("error")
                        error_code = error_info.get("code") if isinstance(error_info, dict) else error_data.get("code")
                except Exception:
                    pass
                if 400 <= status_code < 500:
                    if method == "DELETE" and self.is_already_removed_error(status_code, error_msg, error_code):
                        return {
                            "success": True,
                            "status_code": status_code,
                            "data": {},
                            "error": None,
                            "already_removed": True,
                        }
                    return {
                        "success": False,
                        "status_code": status_code,
                        "error": error_msg,
                        "error_code": error_code,
                    }
                last_error = error_msg
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if self._is_transient(exc) and attempt < self.MAX_RETRIES - 1:
                    rebuilt = await self._rebuild_session(db_session, identifier)
                    if rebuilt is not None:
                        session = rebuilt
                    continue
                return {"success": False, "status_code": 0, "error": last_error, "error_code": "transport"}
            else:
                if attempt < self.MAX_RETRIES - 1:
                    continue
                return {"success": False, "status_code": status_code, "error": last_error, "error_code": error_code}
        return {"success": False, "status_code": 0, "error": "request failed", "error_code": "transport"}

    async def get_wham_usage(
        self,
        access_token: str,
        db_session: DBAsyncSession | None,
        account_id: str | None = None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if account_id:
            headers["chatgpt-account-id"] = str(account_id)
        return await self._make_request(
            "GET",
            f"{self.BASE_URL}/wham/usage",
            headers,
            db_session=db_session,
            identifier=identifier,
        )

    async def refresh_access_token(
        self,
        refresh_token: str,
        client_id: str,
        db_session: DBAsyncSession | None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        if identifier == "default":
            identifier = f"rt_{refresh_token[:8]}"
        primary = await self._make_request(
            "POST",
            "https://auth.openai.com/oauth/token",
            {"Content-Type": "application/json"},
            db_session,
            identifier,
            json_data={
                "client_id": client_id,
                "grant_type": "refresh_token",
                "redirect_uri": "com.openai.sora://auth.openai.com/android/com.openai.sora/callback",
                "refresh_token": refresh_token,
            },
        )
        if primary.get("success"):
            data = primary.get("data") or {}
            return {
                "success": True,
                "access_token": data.get("access_token"),
                "id_token": data.get("id_token"),
                "refresh_token": data.get("refresh_token"),
                "data": data,
            }
        fallback = await self._make_request(
            "POST",
            "https://auth0.openai.com/oauth/token",
            {
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            db_session,
            identifier,
            form_data={
                "grant_type": "refresh_token",
                "client_id": client_id,
                "refresh_token": refresh_token,
                "scope": "openid profile email offline_access",
            },
        )
        if fallback.get("success"):
            data = fallback.get("data") or {}
            return {
                "success": True,
                "access_token": data.get("access_token"),
                "id_token": data.get("id_token"),
                "refresh_token": data.get("refresh_token"),
                "data": data,
            }
        return {
            "success": False,
            "error": (
                f"refresh_token failed. primary={primary.get('error')} ; "
                f"fallback={fallback.get('error')}"
            ),
            "status_code": fallback.get("status_code") or primary.get("status_code"),
            "error_code": fallback.get("error_code") or primary.get("error_code") or "token_refresh_failed",
        }

    def create_oauth_authorize_url(
        self,
        client_id: str,
        redirect_uri: str,
        scope: str = "openid email profile offline_access",
        login_hint: str = "",
    ) -> dict[str, str]:
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("utf-8")).digest()).decode("utf-8").rstrip("=")
        state = secrets.token_urlsafe(24)
        query = {
            "client_id": client_id,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "prompt": "login",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": scope,
            "state": state,
            "codex_cli_simplified_flow": "true",
            "id_token_add_organizations": "true",
        }
        hint = (login_hint or "").strip()
        if hint:
            query["login_hint"] = hint
            query["hint"] = hint
        return {
            "authorize_url": f"https://auth.openai.com/oauth/authorize?{urlencode(query)}",
            "code_verifier": verifier,
            "state": state,
            "client_id": client_id,
        }

    async def exchange_oauth_code(
        self,
        code: str,
        client_id: str,
        redirect_uri: str,
        code_verifier: str,
        db_session: DBAsyncSession | None,
        identifier: str = "oauth_exchange",
    ) -> dict[str, Any]:
        result = await self._make_request(
            "POST",
            "https://auth.openai.com/oauth/token",
            {"Content-Type": "application/json"},
            db_session,
            identifier,
            json_data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        if not result.get("success"):
            return {"success": False, "error": result.get("error") or "code exchange failed", "error_code": result.get("error_code") or "oauth_exchange_failed"}
        data = result.get("data") or {}
        return {
            "success": True,
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
            "id_token": data.get("id_token"),
            "data": data,
        }

    async def get_members(
        self,
        access_token: str,
        account_id: str,
        db_session: DBAsyncSession | None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        all_members: list[dict[str, Any]] = []
        offset = 0
        limit = 50
        while True:
            url = f"{self.BASE_URL}/accounts/{account_id}/users?offset={offset}&limit={limit}&query="
            headers = {
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
            }
            result = await self._make_request("GET", url, headers, db_session=db_session, identifier=identifier)
            if not result.get("success"):
                return {
                    "success": False,
                    "members": [],
                    "total": 0,
                    "error": result.get("error"),
                    "error_code": result.get("error_code"),
                    "status_code": result.get("status_code"),
                }
            data = result.get("data") or {}
            items = data.get("items", []) if isinstance(data, dict) else []
            total = data.get("total", 0) if isinstance(data, dict) else 0
            all_members.extend(item for item in items if isinstance(item, dict))
            if len(all_members) >= int(total or 0):
                break
            offset += limit
        return {"success": True, "members": all_members, "total": len(all_members), "error": None}

    async def get_invites(
        self,
        access_token: str,
        account_id: str,
        db_session: DBAsyncSession | None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}/accounts/{account_id}/invites?offset=0&limit=50&query="
        headers = {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
        }
        result = await self._make_request("GET", url, headers, db_session=db_session, identifier=identifier)
        if not result.get("success"):
            return {
                "success": False,
                "items": [],
                "total": 0,
                "error": result.get("error"),
                "error_code": result.get("error_code"),
                "status_code": result.get("status_code"),
            }
        data = result.get("data") or {}
        items = data.get("items", []) if isinstance(data, dict) else []
        return {"success": True, "items": items, "total": len(items), "error": None}

    async def send_invite(
        self,
        access_token: str,
        account_id: str,
        email: str,
        db_session: DBAsyncSession | None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}/accounts/{account_id}/invites"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
        }
        return await self._make_request(
            "POST",
            url,
            headers,
            db_session=db_session,
            identifier=identifier,
            json_data={"email_addresses": [email], "role": "standard-user", "resend_emails": True},
        )

    async def delete_member(
        self,
        access_token: str,
        account_id: str,
        user_id: str,
        db_session: DBAsyncSession | None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}/accounts/{account_id}/users/{user_id}"
        headers = {
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
        }
        result = await self._make_request("DELETE", url, headers, db_session=db_session, identifier=identifier)
        from app.domain.vacancy import parse_policy_notice

        vacancy = parse_policy_notice(result.get("data"))
        if vacancy is not None:
            result["vacancy"] = vacancy
        return result

    async def delete_invite(
        self,
        access_token: str,
        account_id: str,
        email: str,
        db_session: DBAsyncSession | None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}/accounts/{account_id}/invites"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
        }
        return await self._make_request(
            "DELETE",
            url,
            headers,
            db_session=db_session,
            identifier=identifier,
            json_data={"email_address": email},
        )


chatgpt_client = ChatGPTClient()
