"""codex-rs admin API client. No redirects, no env proxies, no automatic retries.

Secrets never appear in exceptions: error messages only carry HTTP status codes.
"""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from app.application.codex_export import CodexTransferError
from app.application.settings import get_setting_value
from app.application.tokens import decrypt_secret

# Hosts that may use plain HTTP: loopback, plus Docker's alias for the same machine.
PLAIN_HTTP_HOSTS = {"localhost", "127.0.0.1", "::1", "host.docker.internal"}
PAGE_SIZE = 200
MAX_PAGES = 20


def normalize_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("codex-rs 地址无效") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value):
        raise ValueError("codex-rs 地址必须是无凭据、路径和查询参数的 HTTP(S) 服务地址")
    if parsed.scheme == "http" and parsed.hostname.lower() not in PLAIN_HTTP_HOSTS:
        raise ValueError("非本机 codex-rs 地址必须使用 HTTPS")
    host = parsed.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    if port and port != (443 if parsed.scheme == "https" else 80):
        host += f":{port}"
    return urlunsplit((parsed.scheme, host, "", "", ""))


async def load_config(db) -> dict:
    url = await get_setting_value(db, "codex_base_url", "") or ""
    encrypted = await get_setting_value(db, "codex_admin_key_encrypted", "")
    return {"base_url": normalize_url(url) if url else "", "api_key": decrypt_secret(encrypted)}


def _import_settings(settings: dict) -> dict:
    """Team48 defaults (snake_case) -> codex-rs import settings; all four fields are required there."""
    limit = settings.get("concurrency_limit")
    return {"enabled": bool(settings.get("enabled", True)),
            "concurrencyLimit": int(limit) if limit is not None else None,
            "weight": int(settings.get("weight") or 1),
            "groupIds": [str(item) for item in settings.get("group_ids") or []]}


def _text(value) -> str | None:
    return value if isinstance(value, str) and value else None


class CodexClient:
    def __init__(self, base_url: str, api_key: str, *, transport=None):
        if not base_url or not api_key:
            raise CodexTransferError("codex_not_configured", "请先保存 codex-rs 地址和管理员 API Key", 400)
        self.base_url = normalize_url(base_url)
        self.api_key = api_key
        self.transport = transport

    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=self.transport, timeout=15, follow_redirects=False, trust_env=False)

    async def request(self, method: str, path: str, **kwargs):
        """Return the envelope's ``data`` (dict or list). Any 2xx is accepted (import returns 201)."""
        try:
            async with self._http() as client:
                response = await client.request(method, self.base_url + path,
                    headers={"x-api-key": self.api_key, "Accept": "application/json"}, **kwargs)
            status = response.status_code
            if not 200 <= status < 300:
                if status == 404:
                    raise CodexTransferError("remote_not_found", "codex-rs 返回 HTTP 404，未自动重试", 502)
                if status in {401, 403}:
                    raise CodexTransferError("codex_auth_failed", f"codex-rs 拒绝了管理员 API Key（HTTP {status}）", 502)
                if 400 <= status < 500:
                    # A definite rejection: codex-rs validated and refused the request.
                    raise CodexTransferError("codex_rejected", f"codex-rs 拒绝了请求（HTTP {status}），未自动重试", 502)
                raise CodexTransferError("codex_http_error", f"codex-rs 返回 HTTP {status}，未自动重试", 502)
            body = response.json()
            if (not isinstance(body, dict) or body.get("code") != 200
                    or not isinstance(body.get("data"), (dict, list))):
                raise ValueError("unexpected envelope")
            return body["data"]
        except CodexTransferError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise CodexTransferError("codex_response_uncertain", "codex-rs 请求失败或响应无法确认，未自动重试", 502) from None

    async def _object(self, method: str, path: str, **kwargs) -> dict:
        data = await self.request(method, path, **kwargs)
        if not isinstance(data, dict):
            raise CodexTransferError("codex_response_uncertain", "codex-rs 响应格式无法确认", 502)
        return data

    async def _pages(self, path: str, **params) -> list[dict]:
        items: list[dict] = []
        for page in range(1, MAX_PAGES + 1):
            data = await self._object("GET", path, params={**params, "page": page, "pageSize": PAGE_SIZE})
            batch, meta = data.get("items"), data.get("page")
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch) or not isinstance(meta, dict):
                raise CodexTransferError("codex_invalid_list", "codex-rs 列表响应不完整", 502)
            items.extend(batch)
            total = meta.get("total")
            if not batch or type(total) is not int or len(items) >= total:
                return items
        raise CodexTransferError("codex_list_limit", "codex-rs 列表过长，未读取完整", 502)

    async def health(self) -> None:
        try:
            async with self._http() as client:
                response = await client.get(self.base_url + "/healthz")
        except httpx.HTTPError:
            raise CodexTransferError("codex_unreachable", "无法连接 codex-rs，请检查地址和网络", 502) from None
        if not 200 <= response.status_code < 300:
            raise CodexTransferError("codex_unhealthy", f"codex-rs 健康检查返回 HTTP {response.status_code}", 502)

    async def detail(self, remote_id: str) -> dict:
        data = await self._object("GET", "/api/admin/accounts/detail", params={"accountId": remote_id})
        account = data.get("account")
        if not isinstance(account, dict) or account.get("id") != remote_id:
            raise CodexTransferError("codex_invalid_detail", "codex-rs 账号详情响应不完整", 502)
        return account

    async def import_accounts(self, document: dict, *, settings: dict | None = None,
                              proxy_id: str | None = None) -> list[str]:
        """Import (or upsert by token identity) one account document; returns codex-rs account IDs.

        ``settings`` / ``proxy_id`` overwrite an existing account's settings, so callers only
        pass them on the first import.
        """
        body: dict = {"provider": "openai", "data": {"accounts": [document]}}
        if settings is not None:
            body["settings"] = _import_settings(settings)
        if proxy_id:
            body["outboundProxyId"] = proxy_id
        data = await self._object("POST", "/api/admin/accounts/import", json=body)
        ids = data.get("accountIds")
        if (data.get("importedCount") != 1 or not isinstance(ids, list) or len(ids) != 1
                or not isinstance(ids[0], str) or not ids[0]):
            raise CodexTransferError("codex_response_uncertain", "codex-rs 导入响应无法确认", 502)
        return ids

    async def export_tokens(self, remote_id: str) -> dict:
        """Read the current AT / ID token. The refresh token in the export is dropped here."""
        data = await self._object("GET", "/api/admin/accounts/export", params={
            "accountIds": remote_id, "confirm": "export_sensitive_accounts"})
        documents = data.get("documents")
        if not isinstance(documents, list) or len(documents) != 1 or not isinstance(documents[0], dict):
            raise CodexTransferError("codex_invalid_export", "codex-rs 导出响应不完整", 502)
        document = documents[0].get("document")
        # The CPR export wraps entries as {"sourceFormat": "cpr", "accounts": [...]}.
        entries = document.get("accounts") if isinstance(document, dict) and "accounts" in document else [document]
        if not isinstance(entries, list):
            raise CodexTransferError("codex_invalid_export", "codex-rs 导出响应不完整", 502)
        entry = next((item for item in entries if isinstance(item, dict) and item.get("id") == remote_id), None)
        if entry is None or not _text(entry.get("accessToken")):
            raise CodexTransferError("codex_invalid_export", "codex-rs 导出缺少访问令牌", 502)
        return {"id": remote_id, "access_token": entry["accessToken"], "id_token": _text(entry.get("idToken")),
                "expires_at": _text(entry.get("accessTokenExpiresAt")), "email": _text(entry.get("email")),
                "account_id": _text(entry.get("accountId")), "user_id": _text(entry.get("userId"))}

    async def set_enabled(self, remote_id: str, enabled: bool) -> None:
        if enabled:
            await self._object("POST", "/api/admin/accounts/recover", json={"accountId": remote_id})
        else:
            await self._object("POST", "/api/admin/accounts/batch-update",
                               json={"accountIds": [remote_id], "enabled": False})

    async def list_accounts(self) -> list[dict]:
        return await self._pages("/api/admin/accounts", provider="openai")

    async def list_proxies(self) -> list[dict]:
        return await self._pages("/api/admin/proxies")

    async def list_groups(self) -> list[dict]:
        return await self._pages("/api/admin/account-groups")
