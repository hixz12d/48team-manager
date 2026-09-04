"""Idempotent local ProxyProfile to Sub2API proxy synchronization."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.operations import operation_store
from app.application.resources.proxies import proxy_profile_service
from app.core.proxy import split_proxy_url
from app.core.time import isoformat, utcnow
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account
from app.persistence.models.resources import ProxyProfile
from app.persistence.models.sub2api import Sub2ApiProxyBinding


def _is_remote_missing(exc: Exception) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 404


def _safe_error(exc: BaseException | str) -> str:
    text = str(exc or "Sub2API proxy sync failed")
    text = re.sub(r"(https?://|socks5h?://)[^/@\s]+@", r"\1***@", text)
    return text[:500]


class Sub2ApiProxyService:
    def _payload(self, profile: ProxyProfile) -> tuple[dict[str, Any], str]:
        parts = split_proxy_url(proxy_profile_service.compose(profile))
        payload = {
            "name": f"team48:{profile.id} {(profile.name or f'{profile.host}:{profile.port}')}"[:100],
            "protocol": parts["scheme"],
            "host": parts["host"],
            "port": int(parts["port"]),
            "username": parts["username"],
            "password": parts["password"],
            "status": "inactive" if profile.status == "disabled" else "active",
        }
        digest = hashlib.sha256(
            json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return payload, digest

    async def _mapping(
        self, db: AsyncSession, profile_id: int
    ) -> Sub2ApiProxyBinding | None:
        return (
            await db.execute(
                select(Sub2ApiProxyBinding).where(
                    Sub2ApiProxyBinding.local_proxy_profile_id == int(profile_id)
                )
            )
        ).scalar_one_or_none()

    def serialize(self, row: Sub2ApiProxyBinding | None) -> dict[str, Any]:
        if row is None:
            return {
                "remote_proxy_id": None,
                "sync_state": "unbound",
                "last_synced_at": None,
                "last_error": None,
            }
        return {
            "remote_proxy_id": row.remote_proxy_id,
            "sync_state": row.sync_state,
            "last_synced_at": isoformat(row.last_synced_at),
            "last_error": row.last_error or None,
        }

    async def sync_profile(
        self,
        db: AsyncSession,
        profile_id: int,
        *,
        test_before_use: bool = False,
        create_operation: bool = True,
        source: str = "manual",
    ) -> dict[str, Any]:
        profile = await db.get(ProxyProfile, int(profile_id))
        if profile is None:
            return {"ok": False, "error": "proxy not found", "error_code": "not_found"}
        operation = None
        if create_operation:
            operation = await operation_store.create(
                db,
                op_type="sub2api_proxy_sync",
                source=source,
                input_payload={"proxy_profile_id": profile.id, "test_before_use": test_before_use},
                resolved_proxy_profile_id=profile.id,
            )
        mapping = await self._mapping(db, profile.id)
        if mapping is None:
            mapping = Sub2ApiProxyBinding(
                local_proxy_profile_id=profile.id,
                sync_state="pending",
            )
            db.add(mapping)
            await db.flush()

        payload, config_hash = self._payload(profile)
        action = "unchanged"
        remote_id = int(mapping.remote_proxy_id) if str(mapping.remote_proxy_id or "").isdigit() else 0
        try:
            remote = None
            if remote_id:
                try:
                    remote = await sub2api_client.get_proxy(db, remote_id)
                except Exception as exc:
                    if not _is_remote_missing(exc):
                        raise
                    mapping.remote_proxy_id = None
                    mapping.sync_state = "missing"
                    mapping.last_error = "remote proxy missing; rebuilding"
                    remote_id = 0
            if not remote_id:
                created = await sub2api_client.create_proxy(
                    db,
                    payload,
                    idempotency_key=f"team48-proxy-{profile.id}-{config_hash}",
                )
                remote_id = int(created.get("id") or 0)
                if not remote_id:
                    raise RuntimeError("Sub2API proxy create response has no id")
                mapping.remote_proxy_id = str(remote_id)
                action = "create" if mapping.sync_state != "missing" else "recreate"
            elif mapping.last_pushed_hash != config_hash:
                await sub2api_client.update_proxy(db, remote_id, payload)
                action = "update"

            test_result = None
            if test_before_use:
                test_result = await sub2api_client.test_proxy(db, remote_id)
            now = utcnow()
            mapping.sync_state = "synced"
            mapping.last_pushed_hash = config_hash
            mapping.last_synced_at = now
            mapping.last_error = None
            mapping.updated_at = now
            result = {
                "success": True,
                "ok": True,
                "status": "success",
                "action": action,
                "local_proxy_profile_id": profile.id,
                "remote_proxy_id": remote_id,
                "tested": bool(test_before_use),
                "test": test_result,
                "mapping": self.serialize(mapping),
            }
            if operation is not None:
                result["operation_id"] = operation.public_id
                await operation_store.mark_step(
                    db, operation, "sub2api_proxy_sync", state="success", result=result
                )
                await operation_store.finish(db, operation, result)
            await db.commit()
            return result
        except Exception as exc:
            error = _safe_error(exc)
            mapping.sync_state = "failed"
            mapping.last_error = error
            mapping.updated_at = utcnow()
            result = {
                "success": False,
                "ok": False,
                "status": "failed",
                "error_code": "proxy_sync_failed",
                "error": error,
                "message": "代理同步失败",
                "local_proxy_profile_id": profile.id,
                "remote_proxy_id": mapping.remote_proxy_id,
            }
            if operation is not None:
                result["operation_id"] = operation.public_id
                await operation_store.mark_step(
                    db,
                    operation,
                    "sub2api_proxy_sync",
                    state="failed",
                    result=result,
                    error_message=error,
                )
                await operation_store.finish(db, operation, result)
            await db.commit()
            return result

    async def resolve_for_push(
        self,
        db: AsyncSession,
        account: Account,
        *,
        proxy_source: str = "account",
        proxy_profile_id: int | None = None,
        template_id: str | None = None,
        test_before_use: bool = False,
    ) -> dict[str, Any]:
        source = str(proxy_source or "account").strip().lower()
        if source not in {"account", "selected", "template", "none"}:
            raise ValueError("invalid proxy_source")

        selected_id = int(proxy_profile_id) if proxy_profile_id else None
        resolved_source = "selected" if selected_id else source
        if selected_id is None and source == "account":
            selected_id = account.proxy_profile_id
            if selected_id is None and account.proxy:
                profile = await proxy_profile_service.upsert_from_url(
                    db, account.proxy, bound_account=account
                )
                account.proxy_profile_id = profile.id
                selected_id = profile.id
        if selected_id is None and source == "template":
            if not template_id:
                raise ValueError("template proxy requires template_id")
            capabilities = await sub2api_client.integration_capabilities(db)
            supported = (capabilities.get("account_templates") or {}).get("create_from_template")
            if not supported:
                raise ValueError("当前 Sub2API 不支持账号模板代理继承")
            return {
                "source": "template",
                "local_proxy_profile_id": None,
                "remote_proxy_id": None,
                "explicit": False,
            }
        if selected_id is None:
            return {
                "source": "none",
                "local_proxy_profile_id": None,
                "remote_proxy_id": None,
                "explicit": source == "none",
            }

        synced = await self.sync_profile(
            db,
            selected_id,
            test_before_use=test_before_use,
            create_operation=False,
            source="account_push",
        )
        if not synced.get("ok"):
            raise RuntimeError(synced.get("error") or "proxy sync failed")
        return {
            "source": resolved_source,
            "local_proxy_profile_id": selected_id,
            "remote_proxy_id": int(synced["remote_proxy_id"]),
            "explicit": True,
            "sync_action": synced.get("action"),
        }


sub2api_proxy_service = Sub2ApiProxyService()
