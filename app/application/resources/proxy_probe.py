"""Proxy connectivity probe. Updates health fields only; never auto-disables."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.operations import operation_store
from app.application.resources.proxies import proxy_profile_service
from app.core.proxy import build_httpx_proxy, mask_proxy_url
from app.core.time import utcnow
from app.persistence.models.resources import ProxyProfile

DEFAULT_PROBE_URL = "https://api.ipify.org?format=json"
DEFAULT_REGION_URL = "https://ipapi.co/{ip}/json/"
DEFAULT_TIMEOUT = 12.0
DEFAULT_CONCURRENCY = 3


def _safe_error(exc: BaseException, proxy_url: str = "") -> str:
    text = str(exc or "proxy probe failed")
    masked = mask_proxy_url(proxy_url) if proxy_url else ""
    if proxy_url:
        text = text.replace(proxy_url, masked or "***")
    for token in ("password", "passwd", "pwd"):
        if token in text.lower() and "@" in text:
            text = mask_proxy_url(proxy_url) or "proxy probe failed"
            break
    return text[:500]


def _parse_ip(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("ip", "origin", "query"):
            value = payload.get(key)
            if value:
                return str(value).split(",")[0].strip()
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("{"):
            try:
                return _parse_ip(json.loads(text))
            except Exception:
                return ""
        return text.split(",")[0].strip()
    return ""


def _parse_region(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    parts = [
        str(payload.get("city") or "").strip(),
        str(payload.get("region") or payload.get("region_code") or "").strip(),
        str(payload.get("country_name") or payload.get("country") or "").strip(),
    ]
    return " / ".join(part for part in parts if part)


class ProxyProbeService:
    def __init__(
        self,
        *,
        probe_url: str = DEFAULT_PROBE_URL,
        region_url: str = DEFAULT_REGION_URL,
        timeout: float = DEFAULT_TIMEOUT,
        concurrency: int = DEFAULT_CONCURRENCY,
    ) -> None:
        self.probe_url = probe_url
        self.region_url = region_url
        self.timeout = timeout
        self.concurrency = max(1, int(concurrency))

    async def _request_exit_ip(self, proxy_url: str) -> tuple[str, int]:
        proxy = build_httpx_proxy(proxy_url)
        started = time.perf_counter()
        async with httpx.AsyncClient(proxy=proxy, timeout=self.timeout, trust_env=False, follow_redirects=True) as client:
            response = await client.get(self.probe_url)
            response.raise_for_status()
            latency_ms = int((time.perf_counter() - started) * 1000)
            try:
                payload = response.json()
            except Exception:
                payload = response.text
            ip = _parse_ip(payload)
            if not ip:
                raise RuntimeError("probe response missing exit ip")
            return ip, latency_ms

    async def _request_region(self, proxy_url: str, ip: str) -> str:
        if not ip:
            return ""
        proxy = build_httpx_proxy(proxy_url)
        url = self.region_url.format(ip=ip)
        try:
            async with httpx.AsyncClient(proxy=proxy, timeout=min(self.timeout, 8.0), trust_env=False) as client:
                response = await client.get(url)
                if response.status_code >= 400:
                    return ""
                return _parse_region(response.json())
        except Exception:
            return ""

    async def probe_profile(self, db: AsyncSession, profile_id: int, *, create_operation: bool = True) -> dict[str, Any]:
        profile = await db.get(ProxyProfile, int(profile_id))
        if profile is None:
            return {"ok": False, "error": "proxy not found", "error_code": "not_found"}
        proxy_url = proxy_profile_service.compose(profile)
        masked = mask_proxy_url(proxy_url)
        operation = None
        if create_operation:
            operation = await operation_store.create(
                db,
                op_type="proxy_check",
                input_payload={"proxy_profile_id": profile.id, "proxy": masked},
                resolved_proxy=proxy_url,
                resolved_proxy_profile_id=profile.id,
            )
            await operation_store.note(db, operation, "probe", f"probing {masked}")

        stamp = utcnow()
        try:
            exit_ip, latency_ms = await self._request_exit_ip(proxy_url)
            region = await self._request_region(proxy_url, exit_ip)
            profile.health_state = "healthy"
            profile.last_exit_ip = exit_ip
            profile.region = region or profile.region
            profile.latency_ms = latency_ms
            profile.last_checked_at = stamp
            profile.last_success_at = stamp
            profile.last_error = None
            profile.failure_count = 0
            profile.updated_at = stamp
            result = {
                "ok": True,
                "success": True,
                "status": "success",
                "proxy_profile_id": profile.id,
                "health_state": profile.health_state,
                "last_exit_ip": exit_ip,
                "region": profile.region or "",
                "latency_ms": latency_ms,
                "enabled": profile.status,
            }
            if operation is not None:
                await operation_store.mark_step(db, operation, "probe", state="success", result=result)
                await operation_store.finish(db, operation, result)
                result["operation_id"] = operation.public_id
            await db.commit()
            return result
        except Exception as exc:
            message = _safe_error(exc, proxy_url)
            profile.health_state = "failed"
            profile.last_checked_at = stamp
            profile.last_error = message
            profile.failure_count = int(profile.failure_count or 0) + 1
            profile.updated_at = stamp
            # Do not change status/enabled on probe failure.
            result = {
                "ok": False,
                "success": False,
                "status": "failed",
                "proxy_profile_id": profile.id,
                "health_state": profile.health_state,
                "enabled": profile.status,
                "error": message,
                "error_code": "probe_failed",
                "failure_count": profile.failure_count,
            }
            if operation is not None:
                await operation_store.mark_step(
                    db,
                    operation,
                    "probe",
                    state="failed",
                    error_code="probe_failed",
                    error_message=message,
                )
                await operation_store.finish(db, operation, result)
                result["operation_id"] = operation.public_id
            await db.commit()
            return result

    async def probe_all(self, db: AsyncSession) -> dict[str, Any]:
        rows = list((await db.execute(select(ProxyProfile).where(ProxyProfile.status == "active").order_by(ProxyProfile.id.asc()))).scalars())
        operation = await operation_store.create(
            db,
            op_type="proxy_check",
            input_payload={"mode": "probe_all", "count": len(rows)},
        )
        await operation_store.note(db, operation, "probe_all", f"probing {len(rows)} proxies")
        sem = asyncio.Semaphore(self.concurrency)
        results: list[dict[str, Any]] = []
        # Sequential under shared session to keep SQLite safe; semaphore still caps burst.
        for row in rows:
            async with sem:
                results.append(await self.probe_profile(db, row.id, create_operation=False))

        ok_count = sum(1 for item in results if item.get("ok"))
        fail_count = len(results) - ok_count
        payload = {
            "ok": fail_count == 0,
            "success": fail_count == 0,
            "status": "success" if fail_count == 0 else "failed",
            "total": len(results),
            "healthy": ok_count,
            "failed": fail_count,
            "items": [
                {
                    "proxy_profile_id": item.get("proxy_profile_id"),
                    "ok": bool(item.get("ok")),
                    "health_state": item.get("health_state"),
                    "last_exit_ip": item.get("last_exit_ip"),
                    "error": item.get("error"),
                }
                for item in results
            ],
        }
        await operation_store.mark_step(db, operation, "probe_all", state="success" if fail_count == 0 else "failed", result=payload)
        await operation_store.finish(db, operation, payload)
        await db.commit()
        payload["operation_id"] = operation.public_id
        return payload


proxy_probe_service = ProxyProbeService()
