"""Resolve Sub2API proxy references into server-only runtime snapshots."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.proxy import compose_proxy_url, normalize_proxy_url
from app.integrations.sub2api.client import sub2api_client


class ProxyResolutionError(ValueError):
    def __init__(self, message: str, *, error_code: str = "proxy_unresolvable"):
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class RuntimeProxy:
    source: str
    remote_id: int
    instance_key: str
    url: str


def sub2api_instance_key(base_url: str) -> str:
    normalized = str(base_url or "").strip().rstrip("/").lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


def _runtime_url(remote: dict[str, Any]) -> str:
    for key in ("url", "proxy_url", "server_url"):
        raw = str(remote.get(key) or "").strip()
        if not raw:
            continue
        if "***" in raw:
            raise ProxyResolutionError("Sub2API 返回的是脱敏代理，无法用于服务器任务")
        try:
            return normalize_proxy_url(raw) or ""
        except ValueError as exc:
            raise ProxyResolutionError("Sub2API 代理 URL 格式无效") from exc

    scheme = str(remote.get("protocol") or remote.get("scheme") or "").strip().lower()
    host = str(remote.get("host") or remote.get("hostname") or "").strip()
    try:
        port = int(remote.get("port"))
    except (TypeError, ValueError) as exc:
        raise ProxyResolutionError("Sub2API 代理缺少有效端口") from exc
    auth = remote.get("auth") if isinstance(remote.get("auth"), dict) else {}
    username = str(remote.get("username") or auth.get("username") or "")
    password = str(remote.get("password") or auth.get("password") or "")
    if "***" in username or "***" in password:
        raise ProxyResolutionError("Sub2API 未提供可运行的代理认证信息")
    if not scheme or not host:
        raise ProxyResolutionError("Sub2API 代理缺少协议或地址")
    try:
        return normalize_proxy_url(
            compose_proxy_url(
                scheme=scheme,
                host=host,
                port=port,
                username=username,
                password=password,
            )
        ) or ""
    except (TypeError, ValueError) as exc:
        raise ProxyResolutionError("Sub2API 代理配置无效") from exc


async def resolve_sub2api_proxy(db: AsyncSession, remote_id: int) -> RuntimeProxy:
    try:
        wanted = int(remote_id)
    except (TypeError, ValueError) as exc:
        raise ProxyResolutionError("无效的 Sub2API 代理 ID", error_code="invalid_remote_id") from exc
    if wanted <= 0:
        raise ProxyResolutionError("无效的 Sub2API 代理 ID", error_code="invalid_remote_id")

    cfg = await sub2api_client.load_config(db)
    remotes = await sub2api_client.list_proxies(db, cfg)
    remote = next((item for item in remotes if sub2api_client.remote_id(item) == wanted), None)
    if remote is None:
        raise ProxyResolutionError("Sub2API 代理不存在", error_code="proxy_not_found")
    if str(remote.get("status") or "active").strip().lower() in {"disabled", "inactive", "deleted"}:
        raise ProxyResolutionError("Sub2API 代理已停用", error_code="proxy_disabled")
    return RuntimeProxy(
        source="sub2api",
        remote_id=wanted,
        instance_key=sub2api_instance_key(cfg["base_url"]),
        url=_runtime_url(remote),
    )
