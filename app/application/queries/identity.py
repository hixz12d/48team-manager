"""Identity read models. Queries never call OpenAI, Sub2API, or Playwright."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

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


async def overview_query(db: AsyncSession) -> dict[str, Any]:
    accounts = await identity_repo.list_accounts(db)
    memberships = await identity_repo.list_memberships(db)
    bindings = await identity_repo.list_bindings(db)
    workspaces = await identity_repo.list_workspaces(db)
    report = build_audit_report(accounts, memberships, bindings, workspaces)
    attention = [
        {
            "account_id": item["account_id"],
            "email": item["email"],
            "result": item["result"],
            "message": "; ".join(item["reasons"]),
        }
        for item in report["findings"]
        if item["result"] in {"conflict", "suspicious"}
    ]
    return {
        "attention": attention,
        "running_operations": [],
        "recent_events": [],
        "healthy": not attention,
        "identity": report["counts"],
        "workspaces": len(workspaces),
        "accounts": len(accounts),
    }


async def workspaces_query(db: AsyncSession) -> dict[str, Any]:
    accounts = await identity_repo.list_accounts(db)
    memberships = await identity_repo.list_memberships(db)
    workspaces = await identity_repo.list_workspaces(db)
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
        items.append(
            {
                "id": workspace.id,
                "name": workspace.name or f"Workspace #{workspace.id}",
                "official_workspace_id": workspace.official_workspace_id,
                "owner_email": owner.email if owner else None,
                "owner_purpose": owner.local_purpose if owner else None,
                "members": len(seated),
                "seat_limit": workspace.seat_limit,
                "quota": None,
                "rotation": "off",
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
            if purpose == "needs_auth":
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
        items.append(
            {
                "id": account.id,
                "email": account.email,
                "purpose": account.local_purpose,
                "official_plan": account.official_plan,
                "workspace": workspace_names[0] if workspace_names else None,
                "quota_7d": None,
                "auth": account.auth_state,
                "sub2api": _binding_label(bindings_by_account.get(account.id, [])),
                "proxy": "set" if account.proxy else "none",
                "state": state,
                "identity": finding["result"] if finding else "unbound",
                "reasons": finding["reasons"] if finding else [],
            }
        )
    return {"items": items, "next_cursor": None}


async def identity_audit_query(db: AsyncSession) -> dict[str, Any]:
    from app.application.identity import audit_identity

    return await audit_identity(db)
