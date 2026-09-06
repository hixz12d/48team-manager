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

from app.core.config import load_settings
from app.core.proxy import build_curl_cffi_proxies, mask_proxy_url, normalize_proxy_url
from app.integrations.openai.member_adapter import extract_item_list, extract_reported_total, extract_seat_metadata, invite_role_payload
from app.persistence.models.identity import Account

logger = logging.getLogger(__name__)


class ChatGPTClient:
    BASE_URL = "https://chatgpt.com/backend-api"
    OAI_CLIENT_VERSION = "prod-eddc2f6ff65fee2d0d6439e379eab94fe3047f72"
    IMPERSONATE = "chrome136"
    MAX_RETRIES = 3
    RETRY_DELAYS = [1, 2, 4]
    MAX_PAGES = 20
    MAX_ITEMS = 1000
    PAGE_LIMIT = 50
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
        self._session_keys: dict[str, str] = {}
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

    def _tls_verify(self):
        bundle = str(load_settings().openai_ca_bundle or "").strip()
        return bundle or True

    def _cache_key(self, identifier: str, proxy: str | None) -> str:
        fingerprint = mask_proxy_url(proxy) if proxy else "direct"
        return f"{identifier}|{fingerprint}"

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
        return AsyncSession(impersonate=self.IMPERSONATE, proxies=proxies, timeout=30, verify=self._tls_verify())

    def _device_id_for(self, identifier: str) -> str:
        if identifier not in self._device_ids:
            self._device_ids[identifier] = str(uuid.uuid4())
        return self._device_ids[identifier]

    async def _get_session(self, db_session: DBAsyncSession | None, identifier: str) -> AsyncSession:
        proxy = await self._proxy_for(db_session, identifier)
        cache_key = self._cache_key(identifier, proxy)
        existing_key = self._session_keys.get(identifier)
        if existing_key and existing_key != cache_key:
            await self.clear_session(identifier)
        if identifier not in self._sessions:
            self._sessions[identifier] = await self._create_session(db_session, identifier)
            self._session_keys[identifier] = cache_key
        return self._sessions[identifier]

    async def clear_session(self, identifier: str) -> None:
        session = self._sessions.pop(identifier, None)
        self._session_keys.pop(identifier, None)
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

    def _page_fingerprint(self, items: list[Any]) -> str:
        return hashlib.sha256(repr(items).encode("utf-8")).hexdigest()

    async def _paginate(
        self,
        *,
        access_token: str,
        account_id: str,
        path: str,
        db_session: DBAsyncSession | None,
        identifier: str,
        item_key: str,
    ) -> dict[str, Any]:
        collected: list[Any] = []
        seen_ids: set[str] = set()
        fingerprints: set[str] = set()
        offset = 0
        reported_total: int | None = None
        seat_meta: dict[str, Any] = {}
        pages = 0
        last_page_short = False
        incomplete = False
        error_code = ""
        error = None
        while pages < self.MAX_PAGES and len(collected) < self.MAX_ITEMS:
            url = f"{self.BASE_URL}/accounts/{account_id}/{path}?offset={offset}&limit={self.PAGE_LIMIT}&query="
            headers = {
                "Authorization": f"Bearer {access_token}",
                "chatgpt-account-id": account_id,
            }
            result = await self._make_request("GET", url, headers, db_session=db_session, identifier=identifier)
            if not result.get("success"):
                return {
                    "success": False,
                    item_key: [],
                    "items": [],
                    "members": [],
                    "total": 0,
                    "reported_total": reported_total,
                    "raw_item_count": len(collected),
                    "incomplete": True,
                    "error": result.get("error"),
                    "error_code": result.get("error_code") or "fetch_failed",
                    "status_code": result.get("status_code"),
                    "seat_metadata": seat_meta,
                }
            data = result.get("data") or {}
            page_items = extract_item_list(data)
            page_total = extract_reported_total(data)
            if page_total is not None:
                reported_total = page_total
            seat_meta.update(extract_seat_metadata(data))
            if not page_items:
                if pages == 0 and (reported_total or 0) > 0:
                    incomplete = True
                    error_code = "schema_mismatch"
                    error = "official response reported items but the page envelope was empty"
                break
            fingerprint = self._page_fingerprint(page_items)
            if fingerprint in fingerprints:
                return {
                    "success": False,
                    item_key: collected,
                    "items": collected,
                    "members": collected,
                    "total": reported_total if reported_total is not None else len(collected),
                    "reported_total": reported_total,
                    "raw_item_count": len(collected),
                    "incomplete": True,
                    "error": "official pagination repeated a page",
                    "error_code": "incomplete",
                    "seat_metadata": seat_meta,
                }
            fingerprints.add(fingerprint)
            for item in page_items:
                if not isinstance(item, dict):
                    collected.append(item)
                    continue
                identity = str(item.get("id") or item.get("user_id") or item.get("email") or item.get("email_address") or "")
                nested = item.get("user") if isinstance(item.get("user"), dict) else {}
                if not identity:
                    identity = str(nested.get("id") or nested.get("email") or "")
                if identity and identity in seen_ids:
                    continue
                if identity:
                    seen_ids.add(identity)
                collected.append(item)
            pages += 1
            last_page_short = len(page_items) < self.PAGE_LIMIT
            if last_page_short:
                break
            if reported_total is not None and len(collected) >= reported_total:
                break
            offset += self.PAGE_LIMIT
        else:
            if pages >= self.MAX_PAGES or len(collected) >= self.MAX_ITEMS:
                incomplete = True
                error_code = "incomplete"
                error = "official pagination exceeded safety limits"
        if reported_total is not None and reported_total > 0 and len(collected) < reported_total and not incomplete:
            if last_page_short:
                logger.warning(
                    "official %s reported_total=%s collected=%s on a short last page; trusting items",
                    path,
                    reported_total,
                    len(collected),
                )
            else:
                incomplete = True
                error_code = "incomplete"
                error = "fetched fewer official items than reported_total"
        success = not incomplete
        payload = {
            "success": success,
            item_key: collected,
            "items": collected,
            "members": collected,
            "total": reported_total if reported_total is not None else len(collected),
            "reported_total": reported_total,
            "raw_item_count": len(collected),
            "incomplete": incomplete,
            "error": error,
            "error_code": error_code or None,
            "seat_metadata": seat_meta,
        }
        return payload

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
                elif method == "PATCH":
                    response = await session.patch(url, headers=request_headers, json=json_data or {})
                else:
                    raise ValueError(f"unsupported method {method}")
                status_code = response.status_code
                if 200 <= status_code < 300:
                    try:
                        data = response.json()
                    except Exception:
                        data = {}
                    return {"success": True, "status_code": status_code, "data": data, "error": None, "attempts": attempt + 1}
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
                        "retry_after": response.headers.get("Retry-After"),
                        "attempts": attempt + 1,
                    }
                last_error = error_msg
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if self._is_transient(exc) and attempt < self.MAX_RETRIES - 1:
                    rebuilt = await self._rebuild_session(db_session, identifier)
                    if rebuilt is not None:
                        session = rebuilt
                    continue
                return {"success": False, "status_code": 0, "error": last_error, "error_code": "transport", "attempts": attempt + 1}
            else:
                if attempt < self.MAX_RETRIES - 1:
                    continue
                return {"success": False, "status_code": status_code, "error": last_error, "error_code": error_code,
                        "retry_after": response.headers.get("Retry-After"), "attempts": attempt + 1}
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
        if primary.get("status_code") in {0, 403, 429} or int(primary.get("status_code") or 0) >= 500:
            return primary
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
            "error_code": fallback.get("error_code") or "token_refresh_failed",
            "retry_after": fallback.get("retry_after"),
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

    async def get_accounts_check(
        self,
        access_token: str,
        db_session: DBAsyncSession | None,
        account_id: str | None = None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        """Read-only account context. Feature-detected; 4xx is a miss, not a contract."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if account_id:
            headers["chatgpt-account-id"] = str(account_id)
        return await self._make_request(
            "GET",
            f"{self.BASE_URL}/accounts/check",
            headers,
            db_session=db_session,
            identifier=identifier,
        )

    async def get_account_context(
        self,
        access_token: str,
        db_session: DBAsyncSession | None,
        account_id: str | None = None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        """Try known read-only account-context endpoints. First successful JSON wins."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        }
        if account_id:
            headers["chatgpt-account-id"] = str(account_id)
        candidates = (
            "/accounts/check",
            "/accounts/check/v4-2023-04-27",
            "/me",
        )
        last: dict[str, Any] = {"success": False, "error": "no account-context endpoint succeeded", "error_code": "schema_mismatch"}
        for path in candidates:
            result = await self._make_request(
                "GET",
                f"{self.BASE_URL}{path}",
                headers,
                db_session=db_session,
                identifier=identifier,
            )
            result = dict(result)
            result["endpoint"] = path
            if result.get("success") and isinstance(result.get("data"), (dict, list)):
                return result
            last = result
        return last

    async def get_members(
        self,
        access_token: str,
        account_id: str,
        db_session: DBAsyncSession | None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        return await self._paginate(
            access_token=access_token,
            account_id=account_id,
            path="users",
            db_session=db_session,
            identifier=identifier,
            item_key="members",
        )

    async def get_invites(
        self,
        access_token: str,
        account_id: str,
        db_session: DBAsyncSession | None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        return await self._paginate(
            access_token=access_token,
            account_id=account_id,
            path="invites",
            db_session=db_session,
            identifier=identifier,
            item_key="items",
        )

    async def send_invite(
        self,
        access_token: str,
        account_id: str,
        email: str,
        db_session: DBAsyncSession | None,
        identifier: str = "default",
        role: str = "owner",
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}/accounts/{account_id}/invites"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
        }
        payload_role = invite_role_payload(role)
        return await self._make_request(
            "POST",
            url,
            headers,
            db_session=db_session,
            identifier=identifier,
            json_data={"email_addresses": [email], "role": payload_role, "resend_emails": True},
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

    async def update_member_role(
        self,
        access_token: str,
        account_id: str,
        user_id: str,
        role: str,
        db_session: DBAsyncSession | None,
        identifier: str = "default",
    ) -> dict[str, Any]:
        url = f"{self.BASE_URL}/accounts/{account_id}/users/{user_id}"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
        }
        payload_role = invite_role_payload(role)
        return await self._make_request(
            "PATCH",
            url,
            headers,
            db_session=db_session,
            identifier=identifier,
            json_data={"role": payload_role},
        )

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
