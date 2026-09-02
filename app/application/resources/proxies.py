"""Proxy profiles. Operations freeze a URL so later page edits cannot swap it."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.operations import pack_input, unpack_input
from app.core.crypto import token_cipher
from app.core.proxy import compose_proxy_url, inherit_proxy_url, mask_proxy_url, split_proxy_url
from app.core.time import isoformat, utcnow
from app.persistence.models.operations import Operation
from app.persistence.models.resources import ProxyProfile


def proxy_fingerprint(url: str) -> str:
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()


class ProxyProfileService:
    def serialize(self, row: ProxyProfile) -> dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name or "",
            "scheme": row.scheme,
            "host": row.host,
            "port": row.port,
            "status": row.status,
            "health_state": getattr(row, "health_state", None) or "unchecked",
            "region": row.region or "",
            "last_exit_ip": row.last_exit_ip or "",
            "last_checked_at": isoformat(row.last_checked_at) if getattr(row, "last_checked_at", None) else None,
            "last_success_at": isoformat(row.last_success_at) if getattr(row, "last_success_at", None) else None,
            "latency_ms": getattr(row, "latency_ms", None),
            "last_error": getattr(row, "last_error", None) or "",
            "failure_count": int(row.failure_count or 0),
            "url": mask_proxy_url(self.compose(row)),
        }

    def compose(self, row: ProxyProfile) -> str:
        cipher = token_cipher()
        username = ""
        password = ""
        if row.username_encrypted:
            try:
                username = cipher.decrypt(row.username_encrypted)
            except Exception:
                username = ""
        if row.password_encrypted:
            try:
                password = cipher.decrypt(row.password_encrypted)
            except Exception:
                password = ""
        return compose_proxy_url(
            scheme=row.scheme,
            host=row.host,
            port=int(row.port),
            username=username,
            password=password,
        )

    async def upsert_from_url(self, session: AsyncSession, url: str, *, name: str = "") -> ProxyProfile:
        parts = split_proxy_url(url)
        fingerprint = proxy_fingerprint(parts["url"])
        existing = await session.scalar(select(ProxyProfile).where(ProxyProfile.url_fingerprint == fingerprint))
        if existing:
            return existing
        cipher = token_cipher()
        now = utcnow()
        row = ProxyProfile(
            name=name or f"{parts['host']}:{parts['port']}",
            scheme=parts["scheme"],
            host=parts["host"],
            port=parts["port"],
            username_encrypted=cipher.encrypt(parts["username"]) if parts["username"] else None,
            password_encrypted=cipher.encrypt(parts["password"]) if parts["password"] else None,
            url_fingerprint=fingerprint,
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
        return row

    async def frozen_url(self, session: AsyncSession, job_id: str | None) -> str:
        if not job_id:
            return ""
        row = await session.scalar(select(Operation).where(Operation.public_id == job_id))
        if row is None:
            return ""
        if str(row.resolved_proxy or "").strip():
            return str(row.resolved_proxy).strip()
        payload = unpack_input(row.input_json)
        return str(payload.get("resolved_proxy") or "").strip()

    async def freeze(
        self,
        session: AsyncSession,
        *,
        job_id: str | None = None,
        form_proxy: str = "",
        child_proxy: str = "",
        mother_proxy: str = "",
        fallback_proxy: str = "",
    ) -> tuple[str, int | None]:
        existing = await self.frozen_url(session, job_id)
        if existing:
            profile_id = None
            if job_id:
                row = await session.scalar(select(Operation).where(Operation.public_id == job_id))
                profile_id = int(row.resolved_proxy_profile_id) if row and row.resolved_proxy_profile_id else None
            return existing, profile_id
        url = inherit_proxy_url(form_proxy, child_proxy, mother_proxy, fallback_proxy)
        if not url:
            return "", None
        profile = await self.upsert_from_url(session, url)
        if job_id:
            row = await session.scalar(select(Operation).where(Operation.public_id == job_id))
            if row is not None and not str(row.resolved_proxy or "").strip():
                row.resolved_proxy = url
                row.resolved_proxy_profile_id = profile.id
                payload = unpack_input(row.input_json)
                payload["proxy"] = url
                payload["resolved_proxy"] = url
                payload["resolved_proxy_profile_id"] = profile.id
                row.input_json = pack_input(payload)
                row.updated_at = utcnow()
                await session.flush()
        return url, profile.id

    async def list_profiles(self, session: AsyncSession) -> list[ProxyProfile]:
        return list((await session.execute(select(ProxyProfile).order_by(ProxyProfile.id.asc()))).scalars())


proxy_profile_service = ProxyProfileService()
