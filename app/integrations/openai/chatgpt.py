"""ChatGPT official HTTP. Quota uses /backend-api/wham/usage only."""

from __future__ import annotations

import asyncio
import logging
import random
import uuid
from typing import Any
from urllib.parse import urlparse

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
                if method != "GET":
                    raise ValueError(f"unsupported method {method}")
                response = await session.get(url, headers=request_headers)
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


chatgpt_client = ChatGPTClient()
