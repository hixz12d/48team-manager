"""Read-only adapter for the Sub2API proxy catalog."""

from __future__ import annotations

from typing import Any
import re

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.integrations.sub2api.client import sub2api_client


def _positive_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _safe_text(value: Any, *, limit: int = 255) -> str | None:
    if value in (None, "") or not isinstance(value, (str, int, float, bool)):
        return None
    text = str(value)
    text = re.sub(
        r"(?i)((?:https?|socks5h?)://)[^/@\s]+@",
        r"\1***@",
        text,
    )
    return text[:limit]


class Sub2ApiProxyCatalog:
    """Expose only non-secret fields from the remote proxy catalog."""

    def serialize(self, remote: dict[str, Any]) -> dict[str, Any] | None:
        remote_id = _positive_int(remote.get("id"))
        if remote_id is None:
            return None

        port = _positive_int(remote.get("port"))
        health = remote.get("health")
        if not isinstance(health, (str, int, float, bool)):
            health = remote.get("health_state")

        return {
            "id": remote_id,
            "name": _safe_text(remote.get("name"), limit=120),
            "protocol": _safe_text(remote.get("protocol"), limit=24),
            "host": _safe_text(remote.get("host"), limit=255),
            "port": port,
            "status": _safe_text(remote.get("status"), limit=32),
            "health": _safe_text(health, limit=32),
            "exit_ip": _safe_text(
                remote.get("exit_ip", remote.get("last_exit_ip")), limit=64
            ),
            "checked_at": _safe_text(
                remote.get("checked_at", remote.get("last_checked_at")), limit=64
            ),
            "region": _safe_text(remote.get("region"), limit=120),
        }

    async def list(
        self,
        db: AsyncSession,
        *,
        q: str = "",
        cursor: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        remotes = await sub2api_client.list_proxies(db)
        items = [item for remote in remotes if (item := self.serialize(remote)) is not None]
        query = str(q or "").strip().casefold()
        if query:
            items = [
                item
                for item in items
                if query in " ".join(
                    str(item.get(key) or "")
                    for key in ("id", "name", "protocol", "host", "port", "region", "status")
                ).casefold()
            ]
        start = max(0, int(cursor or 0))
        page_size = min(200, max(1, int(limit or 100)))
        page = items[start : start + page_size]
        next_cursor = start + len(page) if start + len(page) < len(items) else None
        return {
            "items": page,
            "next_cursor": next_cursor,
            "source": "sub2api",
            "complete": len(remotes) < 800,
            "total_loaded": len(items),
        }

    async def probe(self, db: AsyncSession, remote_id: int) -> dict[str, Any]:
        proxy_id = _positive_int(remote_id)
        if proxy_id is None:
            return {
                "ok": False,
                "error_code": "invalid_remote_id",
                "message": "无效的 Sub2API 代理 ID",
            }

        try:
            payload = await sub2api_client.test_proxy(db, proxy_id)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                return {
                    "ok": False,
                    "error_code": "not_found",
                    "message": "Sub2API 代理不存在",
                    "id": proxy_id,
                }
            return {
                "ok": False,
                "error_code": "remote_probe_failed",
                "message": "Sub2API 代理检测失败",
                "id": proxy_id,
            }
        except Exception:
            return {
                "ok": False,
                "error_code": "remote_probe_failed",
                "message": "Sub2API 代理检测失败",
                "id": proxy_id,
            }

        explicit = payload.get("ok", payload.get("success"))
        ok = bool(explicit) if explicit is not None else True
        health = payload.get("health")
        if not isinstance(health, (str, int, float, bool)):
            health = payload.get("health_state")
        latency = _positive_int(payload.get("latency_ms"))
        return {
            "ok": ok,
            "id": proxy_id,
            "status": _safe_text(payload.get("status"), limit=32),
            "health": _safe_text(health, limit=32),
            "exit_ip": _safe_text(
                payload.get("exit_ip", payload.get("last_exit_ip")), limit=64
            ),
            "checked_at": _safe_text(
                payload.get("checked_at", payload.get("last_checked_at")), limit=64
            ),
            "region": _safe_text(payload.get("region"), limit=120),
            "latency_ms": latency,
            "source": "sub2api",
        }


sub2api_proxy_catalog = Sub2ApiProxyCatalog()
