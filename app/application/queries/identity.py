"""Identity read models. Queries never call OpenAI, Sub2API, or Playwright."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.proxy import mask_proxy_url
from app.core.time import isoformat
from app.domain.identity import (
    AUDIT_CONFLICT,
    MEMBERSHIP_STATE_INVITED,
    MEMBERSHIP_STATE_JOINED,
    OFFICIAL_ROLE_OWNER,
)
from app.domain.identity.audit import build_audit_report
from app.persistence.repositories import identity as identity_repo


HIDDEN_ACCOUNT_STATES = {"archived"}
AUTH_NEED_STATES = {"refresh_due", "oauth_required", "phone_required", "manual_required", "deactivated"}


def _account_state(account, finding: dict[str, Any] | None) -> str:
    if finding and finding.get("result") == AUDIT_CONFLICT:
        return "conflict"
    return account.operational_state or "unknown"


def _binding_label(bindings: list) -> str:
    if not bindings:
        return "unbound"
    states = {row.binding_state for row in bindings}
    if "conflict" in states:
        return "conflict"
    if "verified" in states:
        return "verified"
    if "missing" in states:
        return "missing"
    if "pending" in states:
        return "pending"
    return next(iter(states))


def _workspace_health(*, owner, owner_state: str | None, members: int, seat_limit: int | None, status: str | None) -> str:
    if owner_state == "conflict":
        return "identity_conflict"
    if owner is not None and owner.auth_state in AUTH_NEED_STATES:
        return "needs_auth"
    if status in {"error", "expired"}:
        return "billing"
    if seat_limit and members < int(seat_limit):
        return "vacancy"
    return "ok"


async def overview_query(db: AsyncSession) -> dict[str, Any]:
    accounts = await identity_repo.list_accounts(db)
    memberships = await identity_repo.list_memberships(db)
    bindings = await identity_repo.list_bindings(db)
    workspaces = await identity_repo.list_workspaces(db)
    report = build_audit_report(accounts, memberships, bindings, workspaces)
    findings_by_id = {item["account_id"]: item for item in report["findings"]}
    accounts_by_id = {row.id: row for row in accounts}
    members_by_workspace: dict[int, list] = defaultdict(list)
    for row in memberships:
        members_by_workspace[row.workspace_id].append(row)
    attention = []
    for item in report["findings"]:
        if item["result"] not in {"conflict", "suspicious"}:
            continue
        account = accounts_by_id.get(item["account_id"])
        workspace_name = None
        if account is not None:
            owned = [row for row in workspaces if row.owner_account_id == account.id]
            if owned:
                workspace_name = owned[0].name
        attention.append(
            {
                "account_id": item["account_id"],
                "email": item["email"],
                "result": item["result"],
                "message": "; ".join(item["reasons"]),
                "workspace": workspace_name,
                "action": "查看",
            }
        )
    workspace_health = []
    for workspace in workspaces:
        owner = accounts_by_id.get(workspace.owner_account_id) if workspace.owner_account_id else None
        seated = [
            row
            for row in members_by_workspace.get(workspace.id, [])
            if row.membership_state in {MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_INVITED}
            and row.official_role != OFFICIAL_ROLE_OWNER
        ]
        owner_finding = findings_by_id.get(owner.id) if owner is not None else None
        health = _workspace_health(
            owner=owner,
            owner_state=_account_state(owner, owner_finding) if owner is not None else None,
            members=len(seated),
            seat_limit=workspace.seat_limit,
            status=workspace.status,
        )
        workspace_health.append(
            {
                "id": workspace.id,
                "name": workspace.name or f"Workspace #{workspace.id}",
                "members": len(seated),
                "seat_limit": workspace.seat_limit,
                "health": health,
                "status": workspace.status,
            }
        )
    workspace_health.sort(key=lambda item: (item["health"] == "ok", item["name"] or ""))
    return {
        "attention": attention,
        "running_operations": [],
        "recent_events": [],
        "healthy": not attention,
        "identity": report["counts"],
        "workspaces": len(workspaces),
        "accounts": len(accounts),
        "workspace_health": workspace_health[:8],
        "summary": {
            "workspaces": len(workspaces),
            "accounts": len(accounts),
            "attention": len(attention),
            "running_operations": 0,
            "identity_conflicts": int((report["counts"] or {}).get("conflict") or 0),
        },
    }


async def workspaces_query(db: AsyncSession) -> dict[str, Any]:
    accounts = await identity_repo.list_accounts(db)
    memberships = await identity_repo.list_memberships(db)
    bindings = await identity_repo.list_bindings(db)
    workspaces = await identity_repo.list_workspaces(db)
    report = build_audit_report(accounts, memberships, bindings, workspaces)
    findings_by_id = {item["account_id"]: item for item in report["findings"]}
    accounts_by_id = {row.id: row for row in accounts}
    members_by_workspace: dict[int, list] = defaultdict(list)
    for row in memberships:
        members_by_workspace[row.workspace_id].append(row)

    items = []
    for workspace in workspaces:
        owner = accounts_by_id.get(workspace.owner_account_id) if workspace.owner_account_id else None
        members = members_by_workspace.get(workspace.id, [])
        seated = [
            row
            for row in members
            if row.membership_state in {MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_INVITED}
            and row.official_role != OFFICIAL_ROLE_OWNER
        ]
        owner_finding = findings_by_id.get(owner.id) if owner is not None else None
        health = _workspace_health(
            owner=owner,
            owner_state=_account_state(owner, owner_finding) if owner is not None else None,
            members=len(seated),
            seat_limit=workspace.seat_limit,
            status=workspace.status,
        )
        member_items = []
        for row in seated:
            account = accounts_by_id.get(row.account_id)
            if account is None:
                continue
            member_items.append(
                {
                    "id": account.id,
                    "email": account.email,
                    "purpose": account.local_purpose,
                    "official_role": row.official_role,
                    "membership_state": row.membership_state,
                    "auth": account.auth_state,
                    "state": _account_state(account, findings_by_id.get(account.id)),
                }
            )
        member_items.sort(key=lambda item: ((item.get("email") or "").lower(), item.get("id") or 0))
        items.append(
            {
                "id": workspace.id,
                "name": workspace.name or f"Workspace #{workspace.id}",
                "official_workspace_id": workspace.official_workspace_id,
                "owner_email": owner.email if owner else None,
                "owner_purpose": owner.local_purpose if owner else None,
                "owner_auth": owner.auth_state if owner else None,
                "owner_proxy": mask_proxy_url(owner.proxy) if owner and owner.proxy else None,
                "owner_proxy_set": bool(owner and owner.proxy),
                "proxy_profile_id": owner.proxy_profile_id if owner else None,
                "members": len(seated),
                "member_accounts": member_items,
                "seat_limit": workspace.seat_limit,
                "quota": None,
                "quota_available": False,
                "rotation": None,
                "automation": None,
                "automation_available": False,
                "health": health,
                "last_sync": isoformat(workspace.last_official_sync_at),
                "status": workspace.status,
            }
        )
    return {"items": items, "next_cursor": None}


async def accounts_query(db: AsyncSession, purpose: str = "all", include_archived: bool = False) -> dict[str, Any]:
    accounts = await identity_repo.list_accounts(db)
    memberships = await identity_repo.list_memberships(db)
    bindings = await identity_repo.list_bindings(db)
    workspaces = await identity_repo.list_workspaces(db)
    report = build_audit_report(accounts, memberships, bindings, workspaces)
    findings_by_id = {item["account_id"]: item for item in report["findings"]}
    workspaces_by_id = {row.id: row for row in workspaces}
    memberships_by_account: dict[int, list] = defaultdict(list)
    for row in memberships:
        memberships_by_account[row.account_id].append(row)
    bindings_by_account: dict[int, list] = defaultdict(list)
    for row in bindings:
        bindings_by_account[row.local_account_id].append(row)

    items = []
    for account in accounts:
        if not include_archived and account.operational_state in HIDDEN_ACCOUNT_STATES:
            continue
        finding = findings_by_id.get(account.id)
        state = _account_state(account, finding)
        if purpose not in {"all", "", None}:
            if purpose == "conflict" and state != "conflict":
                continue
            if purpose == "archived" and account.operational_state != "archived":
                continue
            if purpose == "needs_auth" and (account.auth_state not in AUTH_NEED_STATES):
                continue
            if purpose == "quota_full":
                pass
            elif purpose not in {"conflict", "archived", "needs_auth", "quota_full"} and account.local_purpose != purpose:
                continue
        active = [
            row
            for row in memberships_by_account.get(account.id, [])
            if row.membership_state in {MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_INVITED}
        ]
        workspace_names = [
            workspaces_by_id[row.workspace_id].name or f"#{row.workspace_id}"
            for row in active
            if row.workspace_id in workspaces_by_id
        ]
        binding_rows = bindings_by_account.get(account.id, [])
        primary_membership = active[0] if active else None
        primary_binding = binding_rows[0] if binding_rows else None
        items.append(
            {
                "id": account.id,
                "email": account.email,
                "purpose": account.local_purpose,
                "official_plan": account.official_plan,
                "official_user_id": account.official_user_id,
                "official_account_id": account.official_account_id,
                "workspace": workspace_names[0] if workspace_names else None,
                "official_role": primary_membership.official_role if primary_membership else None,
                "membership_state": primary_membership.membership_state if primary_membership else None,
                "quota_7d": None,
                "auth": account.auth_state,
                "sub2api": _binding_label(binding_rows),
                "proxy": "set" if account.proxy else "none",
                "proxy_url": mask_proxy_url(account.proxy) if account.proxy else None,
                "proxy_profile_id": account.proxy_profile_id,
                "state": state,
                "identity": finding["result"] if finding else "unbound",
                "reasons": finding["reasons"] if finding else [],
                "has_access_token": bool(account.access_token_encrypted),
                "has_refresh_token": bool(account.refresh_token_encrypted),
                "binding": {
                    "remote_id": primary_binding.remote_account_id if primary_binding else None,
                    "verified_email": primary_binding.verified_email if primary_binding else None,
                    "state": primary_binding.binding_state if primary_binding else None,
                    "last_error": primary_binding.last_error if primary_binding else None,
                },
            }
        )
    return {"items": items, "next_cursor": None}


async def identity_audit_query(db: AsyncSession) -> dict[str, Any]:
    from app.application.identity import audit_identity

    return await audit_identity(db)
