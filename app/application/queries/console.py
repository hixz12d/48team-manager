"""Console read models. Queries never call OpenAI, Sub2API, or Playwright."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.operations import operation_store, serialize_operation
from app.application.queries.identity import accounts_query, overview_query, workspaces_query
from app.application.quota import quota_service
from app.application.resources.hme import list_leases
from app.application.resources.phones import phone_pool_service
from app.application.resources.proxies import proxy_profile_service
from app.application.settings import load_console_settings
from app.core.time import isoformat
from app.domain.automation import ACTIVE_STATES


def _quota_payload(snapshot) -> dict[str, Any]:
    if snapshot is None:
        return {
            "five_hour_used_percent": None,
            "seven_day_used_percent": None,
            "five_hour_reset_at": None,
            "seven_day_reset_at": None,
            "queried_at": None,
            "success": None,
        }
    return {
        "five_hour_used_percent": snapshot.five_hour_used_percent,
        "seven_day_used_percent": snapshot.seven_day_used_percent,
        "five_hour_reset_at": isoformat(snapshot.five_hour_reset_at),
        "seven_day_reset_at": isoformat(snapshot.seven_day_reset_at),
        "queried_at": isoformat(snapshot.queried_at),
        "success": bool(snapshot.success),
    }


def _quota_label(snapshot) -> str | None:
    if snapshot is None or not snapshot.success or snapshot.seven_day_used_percent is None:
        return None
    return f"{snapshot.seven_day_used_percent}%"


async def overview(db: AsyncSession) -> dict[str, Any]:
    payload = await overview_query(db)
    operations = await operation_store.list_recent(db, limit=20)
    running = [serialize_operation(row) for row in operations if row.state in ACTIVE_STATES]
    latest = await quota_service.latest_official_by_accounts(db)
    latest_quota_at = None
    for snap in latest.values():
        if snap.success and snap.queried_at and (latest_quota_at is None or snap.queried_at > latest_quota_at):
            latest_quota_at = snap.queried_at
    payload["running_operations"] = running[:4]
    payload["recent_events"] = [
        {
            "id": row.public_id,
            "operation": row.op_type,
            "status": row.state,
            "target": row.email,
            "updated": isoformat(row.updated_at or row.finished_at or row.started_at or row.created_at),
        }
        for row in operations[:8]
    ]
    summary = dict(payload.get("summary") or {})
    summary["running_operations"] = len(running)
    payload["summary"] = summary
    payload["freshness"] = {
        "official_quota_latest_at": isoformat(latest_quota_at),
        "identity_audit_at": None,
        "resources_checked_at": None,
    }
    return payload


async def workspaces(db: AsyncSession) -> dict[str, Any]:
    payload = await workspaces_query(db)
    for item in payload["items"]:
        item.setdefault("quota", None)
        item.setdefault("quota_available", False)
        item.setdefault("automation", None)
        item.setdefault("automation_available", False)
    return payload


async def accounts(db: AsyncSession, purpose: str = "all", include_archived: bool = False) -> dict[str, Any]:
    payload = await accounts_query(db, purpose=purpose, include_archived=include_archived)
    latest = await quota_service.latest_official_by_accounts(db)
    for item in payload["items"]:
        snap = latest.get(item["id"])
        item["quota_7d"] = _quota_label(snap)
        item["quota"] = _quota_payload(snap)
    if purpose == "quota_full":
        payload["items"] = [
            item
            for item in payload["items"]
            if (item.get("quota") or {}).get("seven_day_used_percent") == 100
        ]
    return payload


async def operations(db: AsyncSession) -> dict[str, Any]:
    rows = await operation_store.list_recent(db)
    return {"items": [serialize_operation(row) for row in rows], "next_cursor": None}


async def phones(db: AsyncSession) -> dict[str, Any]:
    cfg = await phone_pool_service.get_config(db)
    rows = await phone_pool_service.list_phones(db)
    return {"items": [phone_pool_service.serialize(row, cfg) for row in rows], "next_cursor": None}


async def hme(db: AsyncSession) -> dict[str, Any]:
    rows = await list_leases(db)
    return {
        "items": [
            {
                "id": row.id,
                "email": row.email,
                "state": row.local_state,
                "label": row.label_desired or "",
                "pending": bool(row.label_sync_pending),
                "job_id": row.job_id or "",
                "expires_at": isoformat(row.expires_at),
                "last_error": row.last_error or "",
            }
            for row in rows
        ],
        "next_cursor": None,
    }


async def proxies(db: AsyncSession) -> dict[str, Any]:
    # GET stays read-only. Use POST /api/resources/proxies/repair for backfill.
    rows = await proxy_profile_service.list_profiles(db)
    return {"items": [proxy_profile_service.serialize(row) for row in rows], "next_cursor": None}


async def settings_view(db: AsyncSession) -> dict[str, Any]:
    return await load_console_settings(db)
