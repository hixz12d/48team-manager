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
    operations = await operation_store.list_recent(db, limit=40)
    running = [serialize_operation(row) for row in operations if row.state in ACTIVE_STATES]
    latest = await quota_service.latest_official_by_accounts(db)
    latest_quota_at = None
    for snap in latest.values():
        if snap.success and snap.queried_at and (latest_quota_at is None or snap.queried_at > latest_quota_at):
            latest_quota_at = snap.queried_at

    attention = list(payload.get("attention") or [])
    seen = {(item.get("kind"), item.get("account_id"), item.get("email"), item.get("workspace_id"), item.get("operation_id"), item.get("href")) for item in attention}

    def add_attention(item: dict[str, Any]) -> None:
        key = (item.get("kind"), item.get("account_id"), item.get("email"), item.get("workspace_id"), item.get("operation_id"), item.get("href"))
        if key in seen:
            return
        seen.add(key)
        attention.append(item)

    for item in attention:
        item.setdefault("kind", "identity")
        if item.get("account_id") and not item.get("href"):
            item["href"] = f"/accounts?account={item['account_id']}"

    accounts_payload = await accounts_query(db, purpose="all", include_archived=False)
    for account in accounts_payload.get("items") or []:
        if account.get("auth") in {"oauth_required", "manual_required", "deactivated", "phone_required", "refresh_due"}:
            add_attention(
                {
                    "kind": "auth",
                    "account_id": account.get("id"),
                    "email": account.get("email"),
                    "workspace": account.get("workspace"),
                    "workspace_id": account.get("workspace_id"),
                    "result": "warning",
                    "message": f"账号需要处理授权：{account.get('auth')}",
                    "action": "去授权",
                    "href": f"/accounts?account={account.get('id')}",
                }
            )
        if account.get("state") == "conflict" or account.get("identity") in {"conflict", "suspicious"}:
            add_attention(
                {
                    "kind": "identity",
                    "account_id": account.get("id"),
                    "email": account.get("email"),
                    "workspace": account.get("workspace"),
                    "workspace_id": account.get("workspace_id"),
                    "result": account.get("identity") or "conflict",
                    "message": "; ".join(account.get("reasons") or []) or "身份冲突待处理",
                    "action": "查看账号",
                    "href": f"/accounts?account={account.get('id')}",
                }
            )
        if account.get("purpose") == "mother" and not account.get("proxy_url") and account.get("proxy") == "none":
            add_attention(
                {
                    "kind": "proxy",
                    "account_id": account.get("id"),
                    "email": account.get("email"),
                    "workspace": account.get("workspace"),
                    "workspace_id": account.get("workspace_id"),
                    "result": "warning",
                    "message": "母号尚未配置代理",
                    "action": "配置代理",
                    "href": f"/accounts?account={account.get('id')}",
                }
            )

    for row in operations:
        if row.state in {"failed", "cancelled", "manual_required"}:
            add_attention(
                {
                    "kind": "operation",
                    "operation_id": row.public_id,
                    "email": row.email,
                    "workspace_id": row.workspace_id or None,
                    "result": row.state,
                    "message": f"任务 {row.op_type} 状态 {row.state}",
                    "action": "查看任务",
                    "href": f"/operations?op={row.public_id}",
                }
            )

    cfg = await phone_pool_service.get_config(db)
    for phone in await phone_pool_service.list_phones(db):
        if phone.status in {"disabled", "risk", "maxed"}:
            add_attention(
                {
                    "kind": "phone",
                    "email": phone.number,
                    "result": phone.status,
                    "message": f"手机号 {phone.number} 状态 {phone.status}",
                    "action": "查看号码",
                    "href": "/resources/phones",
                }
            )

    for lease in await list_leases(db):
        if lease.label_sync_pending:
            add_attention(
                {
                    "kind": "hme",
                    "email": lease.email,
                    "result": "pending",
                    "message": f"HME 标签待同步：{lease.email}",
                    "action": "去 HME",
                    "href": "/resources/hme",
                }
            )

    for profile in await proxy_profile_service.list_profiles(db):
        health = getattr(profile, "health_state", None) or "unchecked"
        if health in {"failed", "unhealthy"} or profile.status == "disabled":
            add_attention(
                {
                    "kind": "proxy",
                    "email": profile.name or f"{profile.host}:{profile.port}",
                    "result": health,
                    "message": f"代理健康异常：{profile.name or profile.host}",
                    "action": "检测代理",
                    "href": "/resources/proxies",
                }
            )

    payload["attention"] = attention[:30]
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
    summary["attention"] = len(attention)
    payload["summary"] = summary
    payload["healthy"] = not attention
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
