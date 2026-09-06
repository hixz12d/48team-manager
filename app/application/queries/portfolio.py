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
from app.application.sub2api_usage import sub2api_usage_service
from app.application.presenters import build_auth_status
from app.core.proxy import mask_proxy_url
from app.core.time import isoformat
from app.domain.identity import MEMBERSHIP_STATE_INVITED, MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_REMOVED
from app.domain.identity.ids import normalize_email
from app.domain.identity.policy import management_role
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
    auth_status = build_auth_status(item)
    return {
        **item,
        **auth_status,
        **({"needs_auth": item["health"]["needs_auth"], "auth_action": item.get("auth_action")} if item.get("health") else {}),
        "kind": kind,
        "has_access_token": bool(item.get("has_access_token")),
        "quota_risk": _quota_risk(quota),
        "usage": item.get("usage"),
        "usage_note": (
            "Sub2API 用量来自本地快照。"
            if (item.get("usage") or {}).get("available")
            else "Sub2API 尚无成功用量快照，不显示 0。"
        ),
    }


async def portfolio_query(db: AsyncSession) -> dict[str, Any]:
    workspaces_payload = await workspaces_query(db)
    accounts_payload = await accounts_query(db, purpose="all", include_archived=True)
    read_health = await quota_service.health_reader(db)
    accounts_by_id = {item["id"]: item for item in accounts_payload.get("items") or []}
    usage_by_context = await sub2api_usage_service.payloads_by_context(db)

    def usage_for(account_id: int, workspace_id: int | None) -> dict[str, Any] | None:
        exact = usage_by_context.get((int(account_id), workspace_id))
        if exact is not None:
            return exact
        if len(account_contexts.get(int(account_id), set())) <= 1:
            return usage_by_context.get((int(account_id), None))
        return None
    accounts_by_email = {normalize_email(item.get("email")): item for item in accounts_by_id.values()}

    memberships = await identity_repo.list_memberships(db)
    memberships_by_workspace: dict[int, list] = defaultdict(list)
    for row in memberships:
        memberships_by_workspace[row.workspace_id].append(row)
    accounts_raw = {row.id: row for row in await identity_repo.list_accounts(db)}
    workspaces_raw = {row.id: row for row in await identity_repo.list_workspaces(db)}
    account_contexts = defaultdict(set)
    for row in memberships:
        account_contexts[row.account_id].add(row.workspace_id)
    for row in workspaces_raw.values():
        if row.owner_account_id:
            account_contexts[row.owner_account_id].add(row.id)

    groups = []
    assigned_ids: set[int] = set()
    for workspace in workspaces_payload.get("items") or []:
        ws_id = workspace["id"]
        workspace_row = workspaces_raw.get(ws_id)
        owner_email = normalize_email(workspace.get("owner_email"))
        current_children: list[dict[str, Any]] = []
        history: list[dict[str, Any]] = []
        members: list[dict[str, Any]] = []
        mother = None
        for row in memberships_by_workspace.get(ws_id, []):
            account = accounts_by_id.get(row.account_id)
            raw = accounts_raw.get(row.account_id)
            email = normalize_email((account or {}).get("email") or (raw.email if raw else ""))
            role = management_role(workspace_row, row.account_id)
            is_owner = role == "mother"
            if account is None and raw is not None:
                account = {
                    "id": raw.id,
                    "email": raw.email,
                    "purpose": raw.local_purpose,
                    "auth": raw.auth_state,
                    "state": raw.operational_state,
                    **read_health(raw, ws_id),
                    "sub2api": "unbound",
                    "proxy": "set" if raw.proxy else "none",
                    "proxy_url": mask_proxy_url(raw.proxy) if raw.proxy else None,
                    "has_access_token": bool(raw.access_token_encrypted),
                    **build_auth_status(raw),
                }
            if account is None:
                continue
            if row.membership_state != MEMBERSHIP_STATE_REMOVED:
                assigned_ids.add(account["id"])
            health = read_health(raw, ws_id)
            remote = next(
                (
                    item
                    for item in workspace.get("official_members") or []
                    if normalize_email(item.get("email")) == email
                ),
                None,
            )
            membership_state = row.membership_state
            if (
                row.membership_state != MEMBERSHIP_STATE_REMOVED
                and remote
                and remote.get("state") in {MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_INVITED}
            ):
                membership_state = remote["state"]
            kind = "mother" if is_owner else ("history" if membership_state == MEMBERSHIP_STATE_REMOVED else ("invited" if membership_state == MEMBERSHIP_STATE_INVITED else "child"))
            card = _account_card(
                {
                    **account,
                    "workspace_id": ws_id,
                    "official_role": (remote or {}).get("role") or row.official_role,
                    "membership_state": membership_state,
                    "management_role": "mother" if is_owner else "child",
                    "managed": True,
                    **health,
                    "usage": usage_for(account["id"], ws_id),
                    "joined_at": isoformat(row.joined_at),
                    "removed_at": isoformat(row.removed_at),
                },
                kind=kind,
            )
            members.append(card)
            if is_owner:
                mother = card
            elif membership_state == MEMBERSHIP_STATE_REMOVED:
                history.append(card)
            elif membership_state in {MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_INVITED}:
                current_children.append(card)
        if mother is None and workspace.get("owner_account_id"):
            owner_item = accounts_by_id.get(workspace["owner_account_id"])
            if owner_item is not None:
                assigned_ids.add(owner_item["id"])
                health = read_health(accounts_raw[owner_item["id"]], ws_id)
                mother = _account_card(
                    {
                        **owner_item,
                        "workspace_id": ws_id,
                        "official_role": owner_item.get("official_role") or "owner",
                        "membership_state": MEMBERSHIP_STATE_JOINED,
                        "management_role": "mother",
                        "managed": True,
                        **health,
                        "usage": usage_for(owner_item["id"], ws_id),
                    },
                    kind="mother",
                )
                members.insert(0, mother)
        unmanaged = []
        invited = []
        for remote in workspace.get("official_members") or []:
            email = normalize_email(remote.get("email"))
            if bool(owner_email) and email == owner_email:
                continue
            if any(normalize_email(item.get("email")) == email for item in current_children + history + ([mother] if mother else [])):
                continue
            local = accounts_by_email.get(email)
            kind = "invited" if remote.get("state") == "invited" else "unmanaged"
            card = {
                "email": remote.get("email"),
                "kind": kind,
                "official_role": remote.get("role"),
                "membership_state": remote.get("state"),
                "management_role": "child",
                "managed": False,
                "workspace_id": ws_id,
                "account_id": local.get("id") if local else None,
                "id": local.get("id") if local else None,
                "name": remote.get("name"),
                "note": "官方邀请尚未接受" if kind == "invited" else "官方已加入，本地未接入",
                "quota": None,
                "usage": None,
            }
            if kind == "invited":
                invited.append(card)
            else:
                unmanaged.append(card)
            members.append(card)
        risks = [
            _quota_risk(item.get("quota"))
            for item in ([mother] if mother else []) + current_children
            if _quota_risk(item.get("quota"))
        ]
        joined_people = workspace.get("official", {}).get("joined_people_total")
        managed_usage = [item.get("usage") for item in ([mother] if mother else []) + current_children]
        workspace_usage = sub2api_usage_service.aggregate(managed_usage)
        groups.append(
            {
                **workspace,
                "mother": mother,
                "owner_detection": {key: mother.get(key) for key in ("health", "latest_check", "last_success_quota", "quota")} if mother else None,
                "owner_needs_auth": mother.get("needs_auth", False) if mother else workspace.get("owner_needs_auth", False),
                "owner_auth_action": mother.get("auth_action") if mother else workspace.get("owner_auth_action"),
                "current_children": current_children,
                "history": history,
                "unmanaged": unmanaged,
                "invited": invited,
                "members": members,
                "counts": {
                    "joined_people": joined_people,
                    "health_auth": sum(bool(item.get("health", {}).get("needs_auth")) for item in ([mother] if mother else []) + current_children),
                    "health_retry": sum(item.get("health", {}).get("code") in {"rate_limited", "temporary_failure", "parse_error"} for item in ([mother] if mother else []) + current_children),
                    "managed_children": len(current_children),
                    "current_children": len(current_children),
                    "unmanaged": len(unmanaged),
                    "invited": len(invited) + sum(item.get("membership_state") == MEMBERSHIP_STATE_INVITED for item in current_children),
                    "history": len(history),
                },
                "quota_risk": "danger" if "danger" in risks else ("warning" if "warning" in risks else ("ok" if risks else None)),
                "usage": workspace_usage,
                "usage_note": "Workspace 计费按已验证 Binding 的本地快照聚合。" if (workspace_usage or {}).get("available") else "尚无可聚合的 Sub2API 用量快照。",
            }
        )

    unassigned = []
    for item in accounts_payload.get("items") or []:
        if item["id"] in assigned_ids:
            continue
        if not item.get("include_reason") and item.get("state") in HIDDEN_ACCOUNT_STATES:
            continue
        item["usage"] = usage_for(item["id"], None)
        item.update(read_health(accounts_raw[item["id"]], None))
        unassigned.append(_account_card(item, kind="unassigned"))

    unique = {}
    for group in groups:
        for item in group["members"]:
            if not item.get("managed") or item.get("membership_state") == MEMBERSHIP_STATE_REMOVED:
                continue
            if item.get("state") in HIDDEN_ACCOUNT_STATES:
                continue
            entry = unique.setdefault(item["id"], {**item, "contexts": []})
            entry["contexts"].append({**item, "workspace_name": group.get("display_name") or group.get("name")})
    for item in unassigned:
        if item.get("state") not in HIDDEN_ACCOUNT_STATES:
            unique.setdefault(item["id"], {**item, "contexts": []})
    for item in unique.values():
        contexts = item["contexts"]
        if len(contexts) > 1:
            item["usage"] = sub2api_usage_service.aggregate([c.get("usage") for c in contexts])
            item["queued"] = any(c.get("queued") for c in contexts)
            item["next_check_at"] = min((c["next_check_at"] for c in contexts if c.get("next_check_at")), default=None)
            needs = any(c["health"]["needs_auth"] for c in contexts)
            bad = sum(c["health"]["code"] not in {"healthy", "quota_exhausted"} for c in contexts)
            item.update(workspace_id=None, quota={}, last_success_quota=None, latest_check=None,
                health={"code": "partial" if bad else "healthy", "label": f"{bad}/{len(contexts)} 个工作区待处理" if bad else "所有工作区检测正常",
                        "severity": "warning" if bad else "success", "action": "details", "needs_auth": needs},
                needs_auth=needs, auth_action="reauthorize" if needs else None)
    account_items = list(unique.values())
    def has_state(item, codes):
        return any(c.get("health", {}).get("code") in codes for c in item.get("contexts") or [item])
    summary = {"teams": len(groups), "accounts": len(account_items),
               "needs_auth": sum(bool(item.get("needs_auth")) for item in account_items),
               "retry": sum(has_state(item, {"rate_limited", "temporary_failure", "parse_error"}) for item in account_items),
               "attention": sum(item["health"]["code"] not in {"healthy", "disabled"} for item in account_items)}
    return {
        "accounts": account_items,
        "summary": summary,
        "probe_runtime": await quota_service.runtime_summary(db),
        "groups": groups,
        "unassigned": unassigned,
        "usage_available": any(item.get("available") for item in usage_by_context.values()),
        "usage_note": "Sub2API 用量仅来自后台同步的本地快照；缺失和失败不会显示为 0。",
    }
