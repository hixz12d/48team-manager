"""Phase 5：静态 ISP 档案。任务创建时 freeze，不跟页面字符串走。"""
from __future__ import annotations

import hashlib
import logging
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote, unquote, urlparse, urlunparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Operation, ProxyProfile
from app.services.encryption import encryption_service
from app.utils.proxy import normalize_proxy_url
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)


def proxy_fingerprint(url: str) -> str:
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()


def split_proxy_url(url: str) -> Dict[str, Any]:
    normalized = normalize_proxy_url(url)
    if not normalized:
        raise ValueError("代理地址为空")
    parsed = urlparse(normalized)
    if not parsed.hostname or parsed.port is None:
        raise ValueError("代理地址缺少 host/port")
    return {
        "url": normalized,
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": int(parsed.port),
        "username": unquote(parsed.username) if parsed.username is not None else "",
        "password": unquote(parsed.password) if parsed.password is not None else "",
    }


def compose_proxy_url(
    *,
    scheme: str,
    host: str,
    port: int,
    username: str = "",
    password: str = "",
) -> str:
    netloc = f"{host}:{int(port)}"
    if username:
        user = quote(username, safe="")
        if password:
            user = f"{user}:{quote(password, safe='')}"
        netloc = f"{user}@{netloc}"
    return urlunparse((scheme, netloc, "", "", "", ""))


def inherit_proxy_url(*candidates: Optional[str]) -> str:
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            return normalize_proxy_url(text) or text
        except ValueError:
            return text
    return ""


class ProxyProfileService:
    def serialize(self, row: ProxyProfile) -> Dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name or "",
            "scheme": row.scheme,
            "host": row.host,
            "port": row.port,
            "status": row.status,
            "region": row.region or "",
            "last_exit_ip": row.last_exit_ip or "",
            "failure_count": int(row.failure_count or 0),
            "url": self.compose(row),
        }

    def compose(self, row: ProxyProfile) -> str:
        username = ""
        password = ""
        if row.username_encrypted:
            try:
                username = encryption_service.decrypt_token(row.username_encrypted)
            except Exception:
                username = ""
        if row.password_encrypted:
            try:
                password = encryption_service.decrypt_token(row.password_encrypted)
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
        existing = await session.scalar(
            select(ProxyProfile).where(ProxyProfile.url_fingerprint == fingerprint)
        )
        if existing:
            return existing
        now = get_now()
        row = ProxyProfile(
            name=name or f"{parts['host']}:{parts['port']}",
            scheme=parts["scheme"],
            host=parts["host"],
            port=parts["port"],
            username_encrypted=encryption_service.encrypt_token(parts["username"]) if parts["username"] else None,
            password_encrypted=encryption_service.encrypt_token(parts["password"]) if parts["password"] else None,
            url_fingerprint=fingerprint,
            status="active",
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        await session.flush()
        return row

    async def frozen_url(self, session: AsyncSession, job_id: Optional[str]) -> str:
        if not job_id:
            return ""
        row = await session.scalar(select(Operation).where(Operation.public_id == job_id))
        if row is None:
            return ""
        if str(row.resolved_proxy or "").strip():
            return str(row.resolved_proxy).strip()
        from app.services.operations import unpack_input

        payload = unpack_input(row.input_json)
        return str(payload.get("resolved_proxy") or "").strip()

    async def freeze(
        self,
        session: AsyncSession,
        *,
        job_id: Optional[str] = None,
        form_proxy: str = "",
        child_proxy: str = "",
        mother_proxy: str = "",
        fallback_proxy: str = "",
    ) -> Tuple[str, Optional[int]]:
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
            if row is None:
                await session.flush()
                row = await session.scalar(select(Operation).where(Operation.public_id == job_id))
            if row is not None and not str(row.resolved_proxy or "").strip():
                row.resolved_proxy = url
                row.resolved_proxy_profile_id = profile.id
                from app.services.operations import pack_input, unpack_input

                payload = unpack_input(row.input_json)
                payload["proxy"] = url
                payload["resolved_proxy"] = url
                payload["resolved_proxy_profile_id"] = profile.id
                row.input_json = pack_input(payload)
                row.updated_at = get_now()
                await session.flush()
            elif row is None:
                from app.services import onboard_jobs

                onboard_jobs.attach_resume(
                    job_id,
                    {"proxy": url, "resolved_proxy": url, "resolved_proxy_profile_id": profile.id},
                )
        return url, profile.id


proxy_profile_service = ProxyProfileService()
