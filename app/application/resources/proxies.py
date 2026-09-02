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
from app.persistence.models.identity import Account
from app.persistence.models.resources import ProxyProfile


def proxy_fingerprint(url: str) -> str:
    return hashlib.sha256(str(url or "").encode("utf-8")).hexdigest()


class ProxyProfileService:
    def serialize(self, row: ProxyProfile, *, binding_count: int | None = None) -> dict[str, Any]:
        return {
            "id": row.id,
            "name": row.name or "",
            "name_source": getattr(row, "name_source", None) or "auto",
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
            "binding_count": binding_count,
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

    async def upsert_from_url(
        self,
        session: AsyncSession,
        url: str,
        *,
        name: str = "",
        name_source: str = "auto",
        bound_account: Account | None = None,
    ) -> ProxyProfile:
        from app.domain.resources.proxy_names import (
            NAME_SOURCE_AUTO,
            NAME_SOURCE_USER,
            default_name_for_account,
            host_port_label,
            is_legacy_auto_name,
            name_for_bindings,
        )

        parts = split_proxy_url(url)
        fingerprint = proxy_fingerprint(parts["url"])
        existing = await session.scalar(select(ProxyProfile).where(ProxyProfile.url_fingerprint == fingerprint))
        wanted_source = str(name_source or NAME_SOURCE_AUTO).strip().lower() or NAME_SOURCE_AUTO
        if wanted_source not in {NAME_SOURCE_AUTO, NAME_SOURCE_USER}:
            wanted_source = NAME_SOURCE_AUTO
        if existing:
            current_source = str(getattr(existing, "name_source", None) or NAME_SOURCE_AUTO).strip().lower() or NAME_SOURCE_AUTO
            if wanted_source == NAME_SOURCE_USER and name:
                existing.name = name
                existing.name_source = NAME_SOURCE_USER
                existing.updated_at = utcnow()
            elif current_source != NAME_SOURCE_USER:
                # Refresh auto name from current bindings / provided account.
                bindings = list(
                    (
                        await session.execute(
                            select(Account).where(Account.proxy_profile_id == existing.id).order_by(Account.id.asc())
                        )
                    ).scalars()
                )
                if bound_account is not None and all(row.id != bound_account.id for row in bindings):
                    bindings = [*bindings, bound_account]
                if bindings:
                    existing.name = name_for_bindings(host=existing.host, port=existing.port, bindings=bindings)
                elif name:
                    existing.name = name
                elif is_legacy_auto_name(existing.name):
                    existing.name = host_port_label(existing.host, existing.port)
                existing.name_source = NAME_SOURCE_AUTO
                existing.updated_at = utcnow()
            return existing
        cipher = token_cipher()
        now = utcnow()
        if not name and bound_account is not None:
            name = default_name_for_account(
                purpose=bound_account.local_purpose,
                email=bound_account.email,
                host=parts["host"],
                port=parts["port"],
            )
        row = ProxyProfile(
            name=name or f"{parts['host']}:{parts['port']}",
            name_source=wanted_source if name else NAME_SOURCE_AUTO,
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


    async def list_bindings(self, session: AsyncSession, proxy_id: int) -> dict[str, Any]:
        profile = await session.get(ProxyProfile, int(proxy_id))
        if profile is None:
            return {"ok": False, "error": "proxy not found", "error_code": "not_found"}
        rows = list(
            (
                await session.execute(
                    select(Account).where(Account.proxy_profile_id == int(proxy_id)).order_by(Account.id.asc())
                )
            ).scalars()
        )
        items = [
            {
                "id": row.id,
                "email": row.email,
                "purpose": row.local_purpose,
                "auth": row.auth_state,
                "state": row.operational_state,
                "proxy_url": mask_proxy_url(row.proxy) if row.proxy else None,
            }
            for row in rows
        ]
        return {
            "ok": True,
            "proxy": self.serialize(profile, binding_count=len(items)),
            "items": items,
            "count": len(items),
        }

    async def repair_legacy_names(self, session: AsyncSession) -> dict[str, Any]:
        from app.domain.resources.proxy_names import NAME_SOURCE_AUTO, is_legacy_auto_name, name_for_bindings

        rows = await self.list_profiles(session)
        changed = 0
        skipped = 0
        for profile in rows:
            source = str(getattr(profile, "name_source", None) or NAME_SOURCE_AUTO).strip().lower()
            if source == "user":
                skipped += 1
                continue
            if not is_legacy_auto_name(profile.name):
                skipped += 1
                continue
            bindings = list(
                (
                    await session.execute(
                        select(Account).where(Account.proxy_profile_id == profile.id).order_by(Account.id.asc())
                    )
                ).scalars()
            )
            profile.name = name_for_bindings(host=profile.host, port=profile.port, bindings=bindings)
            profile.name_source = NAME_SOURCE_AUTO
            profile.updated_at = utcnow()
            changed += 1
        if changed:
            await session.flush()
        return {"ok": True, "changed": changed, "skipped": skipped}

    async def rename(
        self,
        session: AsyncSession,
        profile: ProxyProfile,
        *,
        name: str | None = None,
        restore_auto_name: bool = False,
    ) -> ProxyProfile:
        from app.domain.resources.proxy_names import NAME_SOURCE_AUTO, NAME_SOURCE_USER, name_for_bindings

        cleaned = str(name).strip() if name is not None else None
        if restore_auto_name or (name is not None and not cleaned):
            bindings = list(
                (
                    await session.execute(
                        select(Account).where(Account.proxy_profile_id == profile.id).order_by(Account.id.asc())
                    )
                ).scalars()
            )
            profile.name = name_for_bindings(host=profile.host, port=profile.port, bindings=bindings)
            profile.name_source = NAME_SOURCE_AUTO
        elif cleaned:
            profile.name = cleaned
            profile.name_source = NAME_SOURCE_USER
        profile.updated_at = utcnow()
        return profile



proxy_profile_service = ProxyProfileService()
