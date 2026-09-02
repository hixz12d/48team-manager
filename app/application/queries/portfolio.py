"""Workspace portfolio read model. Local data only; no live OpenAI/Sub2API calls."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.queries.identity import (
    HIDDEN_ACCOUNT_STATES,
    accounts_query,
    workspaces_query,
)
from app.application.quota import quota_service
from app.core.proxy import mask_proxy_url
from app.core.time import isoformat
from app.domain.identity import LOCAL_PURPOSE_CHILD, LOCAL_PURPOSE_MOTHER, MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_REMOVED
from app.domain.identity.ids import normalize_email
from app.persistence.repositories import identity as identity_repo


def _quota_payload(snapshot) -> dict[str, Any]:
    if snapshot is None:
        return {
            "five_hour_used_percent": None,
            "seven_day_used_percent": None,
            "five_hour_reset_at": None,
            "seven_day_reset_at": None,
            "queried_at": None,
            "success": None,
            "source": None,
        }
    return {
        "five_hour_used_percent": snapshot.five_hour_used_percent,
        "seven_day_used_percent": snapshot.seven_day_used_percent,
        "five_hour_reset_at": isoformat(snapshot.five_hour_reset_at),
        "seven_day_reset_at": isoformat(snapshot.seven_day_reset_at),
        "queried_at": isoformat(snapshot.queried_at),
        "success": bool(snapshot.success),
        "source": snapshot.source,
    }


def _quota_risk(quota: dict[str, Any] | None) -> str | None:
    if not quota or quota.get("success") is False:
        return None
    seven = quota.get("seven_day_used_percent")
    five = quota.get("five_hour_used_percent")
    values = [value for value in (seven, five) if isinstance(value, (int, float))]
    if not values:
        return None
    peak = max(values)
    if peak >= 90:
        return "danger"
    if peak >= 75:
        return "warning"
    return "ok"


def _account_card(item: dict[str, Any], *, kind: str) -> dict[str, Any]:
    quota = item.get("quota") or {}
    return {
        **item,
        "kind": kind,
        "quota_risk": _quota_risk(quota),
        "usage": None,
        "usage_note": "请求量、Token 和账单需独立采集后才显示，当前仅有官方 5h/7d 额度快照。",
    }


async def portfolio_query(db: AsyncSession) -> dict[str, Any]:
    workspaces_payload = await workspaces_query(db)
    accounts_payload = await accounts_query(db, purpose="all", include_archived=True)
    latest = await quota_service.latest_official_by_accounts(db)
    accounts_by_id = {item["id"]: item for item in accounts_payload.get("items") or []}
    for item in accounts_by_id.values():
        snap = latest.get(item["id"])
        item["quota"] = _quota_payload(snap)

    memberships = await identity_repo.list_memberships(db)
    memberships_by_workspace: dict[int, list] = defaultdict(list)
    for row in memberships:
        memberships_by_workspace[row.workspace_id].append(row)
    accounts_raw = {row.id: row for row in await identity_repo.list_accounts(db)}

    groups = []
    assigned_ids: set[int] = set()
    for workspace in workspaces_payload.get("items") or []:
        ws_id = workspace["id"]
        owner_email = normalize_email(workspace.get("owner_email"))
        current_children: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        mother = None
        for row in memberships_by_workspace.get(ws_id, []):
            account = accounts_by_id.get(row.account_id)
            raw = accounts_raw.get(row.account_id)
            email = normalize_email((account or {}).get("email") or (raw.email if raw else ""))
            is_owner = email == owner_email or row.official_role == "owner" or (account or {}).get("purpose") == LOCAL_PURPOSE_MOTHER
            if account is None and raw is not None:
                account = {
                    "id": raw.id,
                    "email": raw.email,
                    "purpose": raw.local_purpose,
                    "auth": raw.auth_state,
                    "state": raw.operational_state,
                    "quota": _quota_payload(latest.get(raw.id)),
                    "sub2api": "unbound",
                    "proxy": "set" if raw.proxy else "none",
                    "proxy_url": mask_proxy_url(raw.proxy) if raw.proxy else None,
                }
            if account is None:
                continue
            assigned_ids.add(account["id"])
            card = _account_card(
                {
                    **account,
                    "workspace_id": ws_id,
                    "official_role": row.official_role,
                    "membership_state": row.membership_state,
                    "joined_at": isoformat(row.joined_at),
                    "removed_at": isoformat(row.removed_at),
                },
                kind="mother" if is_owner else ("history" if row.membership_state == MEMBERSHIP_STATE_REMOVED else "child"),
            )
            if is_owner:
                mother = card
            elif row.membership_state == MEMBERSHIP_STATE_REMOVED:
                history.append(card)
            elif row.membership_state == MEMBERSHIP_STATE_JOINED or account.get("purpose") == LOCAL_PURPOSE_CHILD:
                current_children.append(card)
        unmanaged = []
        for remote in workspace.get("official_members") or []:
            if remote.get("is_owner"):
                continue
            email = normalize_email(remote.get("email"))
            if any(normalize_email(item.get("email")) == email for item in current_children + history + ([mother] if mother else [])):
                continue
            unmanaged.append(
                {
                    "email": remote.get("email"),
                    "kind": "unmanaged",
                    "official_role": remote.get("role"),
                    "membership_state": remote.get("state"),
                    "name": remote.get("name"),
                    "note": "官方已加入，本地未托管",
                    "quota": None,
                    "usage": None,
                }
            )
        risks = [
            _quota_risk(item.get("quota"))
            for item in ([mother] if mother else []) + current_children
            if _quota_risk(item.get("quota"))
        ]
        groups.append(
            {
                **workspace,
                "mother": mother,
                "current_children": current_children,
                "history": history,
                "unmanaged": unmanaged,
                "counts": {
                    "current_children": len(current_children),
                    "history": len(history),
                    "unmanaged": len(unmanaged),
                },
                "quota_risk": "danger" if "danger" in risks else ("warning" if "warning" in risks else ("ok" if risks else None)),
                "usage": None,
                "usage_note": "Workspace 生命周期账单尚未采集，不显示虚构金额。",
            }
        )

    unassigned = []
    for item in accounts_payload.get("items") or []:
        if item["id"] in assigned_ids:
            continue
        if not item.get("include_reason") and item.get("state") in HIDDEN_ACCOUNT_STATES:
            continue
        unassigned.append(_account_card(item, kind="unassigned"))

    return {
        "groups": groups,
        "unassigned": unassigned,
        "usage_available": False,
        "usage_note": "当前只有官方 5h/7d 额度快照。请求量、Token、账号计费和用户扣费需独立账本，未采集时不显示 0。",
    }
