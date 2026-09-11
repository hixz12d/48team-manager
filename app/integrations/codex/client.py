"""Codex admin API client. No redirects, secret responses, or automatic retries."""
from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

import httpx

from app.application.codex_export import CodexTransferError
from app.application.settings import get_setting_value
from app.application.tokens import decrypt_secret


def normalize_url(value: str) -> str:
    value = value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        raise ValueError("Codex 地址无效") from None
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
            or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value):
        raise ValueError("Codex 地址必须是无凭据、路径和查询参数的 HTTP(S) 服务地址")
    if parsed.scheme == "http" and parsed.hostname.lower() not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("非本机 Codex 地址必须使用 HTTPS")
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


class CodexClient:
    def __init__(self, base_url: str, api_key: str, *, transport=None):
        if not base_url or not api_key:
            raise CodexTransferError("codex_not_configured", "请先保存 Codex 地址和管理员 API Key", 400)
        self.base_url = normalize_url(base_url)
        self.api_key = api_key
        self.transport = transport

    async def request(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=15, follow_redirects=False,
                                         trust_env=False) as client:
                response = await client.request(method, self.base_url + path,
                    headers={"x-api-key": self.api_key, "Accept": "application/json"}, **kwargs)
            if not 200 <= response.status_code < 300:
                code = "remote_not_found" if response.status_code == 404 else "codex_http_error"
                raise CodexTransferError(code, f"Codex 返回 HTTP {response.status_code}，未自动重试", 502)
            body = response.json()
            if not isinstance(body, dict) or body.get("code") != 200 or not isinstance(body.get("data"), dict):
                raise ValueError("unexpected envelope")
            return body["data"]
        except CodexTransferError:
            raise
        except (httpx.HTTPError, ValueError, TypeError):
            raise CodexTransferError("codex_response_uncertain", "Codex 请求失败或响应无法确认；保留绑定，未自动重试", 502) from None

    async def detail(self, remote_id: str) -> dict:
        data = await self.request("GET", "/api/admin/accounts/detail", params={"accountId": remote_id})
        account = data.get("account")
        if not isinstance(account, dict) or account.get("id") != remote_id:
            raise CodexTransferError("codex_invalid_detail", "Codex 账号详情响应不完整", 502)
        return account

    async def find_accounts(self, email: str) -> list[dict]:
        items, seen = [], set()
        total = None
        for page in range(1, 21):
            data = await self.request("GET", "/api/admin/accounts", params={
                "provider": "openai", "search": email, "page": page, "pageSize": 100})
            batch, meta = data.get("items"), data.get("page")
            if (not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch)
                    or not isinstance(meta, dict) or meta.get("page") != page or meta.get("pageSize") != 100
                    or type(meta.get("total")) is not int or meta["total"] < 0):
                raise CodexTransferError("codex_invalid_list", "Codex 账号列表响应不完整", 502)
            if total is None:
                total = meta["total"]
            if total > 2000:
                raise CodexTransferError("codex_list_limit", "匹配账号过多，无法安全判断重复账号")
            if total != meta["total"] or len(batch) != min(100, total - len(items)):
                raise CodexTransferError("codex_list_changed", "Codex 列表不完整或正在变化，请稍后重试")
            for item in batch:
                remote_id = item.get("id")
                if not isinstance(remote_id, str) or remote_id in seen:
                    raise CodexTransferError("codex_invalid_list", "Codex 分页账号重复或缺少 ID", 502)
                seen.add(remote_id)
            items.extend(batch)
            if len(items) == total:
                return items
        raise CodexTransferError("codex_list_limit", "匹配账号过多，无法安全判断重复账号")

    async def create(self, document: dict) -> str:
        data = await self.request("POST", "/api/admin/accounts/import",
                                  json={"provider": "openai", "data": {"accounts": [document]}})
        ids = data.get("accountIds")
        if data.get("importedCount") != 1 or not isinstance(ids, list) or len(ids) != 1 or not isinstance(ids[0], str) or not ids[0].startswith("acct_"):
            raise CodexTransferError("codex_response_uncertain", "导入响应无法确认，禁止重复创建", 502)
        return ids[0]

    async def rotate(self, remote_id: str, document: dict):
        await self.request("POST", "/api/admin/accounts/rotate", json={
            "provider": "openai", "accountId": remote_id, "accessToken": document["access_token"],
            "idToken": document.get("id_token"), "refreshToken": None})
