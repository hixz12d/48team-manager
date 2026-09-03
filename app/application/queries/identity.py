"""Identity read models. Queries never call OpenAI, Sub2API, or Playwright."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.presenters import membership_status_label
from app.core.proxy import mask_proxy_url
from app.core.time import isoformat
from app.domain.identity import (
    AUDIT_CONFLICT,
    MEMBERSHIP_STATE_INVITED,
    MEMBERSHIP_STATE_JOINED,
)
from app.domain.identity.audit import build_audit_report
from app.domain.identity.ids import normalize_email
from app.domain.identity.policy import is_workspace_owner
from app.integrations.openai.member_adapter import is_admin_role, is_owner_role, normalize_official_role
from app.domain.workspaces.names import resolve_display_name
from app.persistence.repositories import identity as identity_repo


HIDDEN_ACCOUNT_STATES = {"archived"}
AUTH_NEED_STATES = {"refresh_due", "oauth_required", "phone_required", "manual_required", "deactivated", "unknown"}


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


def _workspace_health(
    *,
    owner,
    owner_state: str | None,
    sync_state: str | None,
    joined_member_count: int | None,
    managed_count: int,
    local_only: int,
    seat_limit: int | None,
    occupied_seats: int | None,
    status: str | None,
) -> str:
    if owner_state == "conflict":
        return "identity_conflict"
    if owner is not None and owner.auth_state in AUTH_NEED_STATES:
        return "needs_auth"
    if sync_state in {"failed", "error"}:
        return "sync_failed"
    if sync_state in {None, "never"}:
        return "not_synced"
    if status in {"error", "expired"}:
        return "billing"
    if local_only > 0:
        return "membership_drift"
    if managed_count == 0 and (joined_member_count or 0) > 0:
        return "needs_management"
    if seat_limit is not None and occupied_seats is not None:
        if occupied_seats >= int(seat_limit):
            return "full"
        if occupied_seats < int(seat_limit):
            return "vacancy"
    return "ok"


def _workspace_display(workspace, owner_email: str | None = None) -> dict[str, Any]:
    return resolve_display_name(workspace, owner_email=owner_email)


def _official_counts(snaps: list, *, owner_email: str) -> dict[str, Any]:
    joined_people_total = 0
    official_owner_count = 0
    official_admin_count = 0
    official_member_count = 0
    invited_count = 0
    primary_mother_count = 0
    for snap in snaps:
        email = normalize_email(snap.normalized_email)
        role = normalize_official_role(snap.official_role)
        if snap.remote_state == "invited":
            invited_count += 1
            continue
        joined_people_total += 1
        if bool(owner_email) and email == owner_email:
            primary_mother_count = 1
        if is_owner_role(role):
            official_owner_count += 1
        elif is_admin_role(role):
            official_admin_count += 1
        else:
            official_member_count += 1
    return {
        "joined_people_total": joined_people_total,
        "owner_count": official_owner_count,
        "official_owner_count": official_owner_count,
        "official_member_count": official_member_count,
        "primary_mother_count": primary_mother_count,
        "joined_member_count": official_member_count,
        "invited_count": invited_count,
    }


def _sync_state_for(workspace, *, has_snapshot: bool) -> str:
    explicit = str(getattr(workspace, "last_official_sync_state", None) or "").strip()
    if explicit:
        return explicit
    if has_snapshot or workspace.last_official_sync_at:
        return "fresh"
    return "never"


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
    snapshots = await identity_repo.list_official_snapshots(db)
    snapshots_by_workspace: dict[int, list] = defaultdict(list)
    for row in snapshots:
        snapshots_by_workspace[row.workspace_id].append(row)

    attention = []
    for item in report["findings"]:
        if item["result"] not in {"conflict", "suspicious"}:
            continue
        account = accounts_by_id.get(item["account_id"])
        workspace_name = None
        if account is not None:
            owned = [row for row in workspaces if row.owner_account_id == account.id]
            if owned:
                workspace_name = _workspace_display(owned[0], owner_email=account.email)["display_name"]
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
        owner_email = normalize_email(owner.email) if owner is not None else ""
        snaps = snapshots_by_workspace.get(workspace.id, [])
        has_snapshot = bool(snaps) or bool(workspace.last_official_sync_at)
        counts = _official_counts(snaps, owner_email=owner_email) if has_snapshot else {
            "joined_people_total": None,
            "owner_count": None,
            "official_owner_count": None,
            "official_member_count": None,
            "primary_mother_count": None,
            "joined_member_count": None,
            "invited_count": None,
        }
        managed = [
            row
            for row in members_by_workspace.get(workspace.id, [])
            if row.membership_state in {MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_INVITED}
            and not is_workspace_owner(workspace, row.account_id)
        ]
        owner_finding = findings_by_id.get(owner.id) if owner is not None else None
        display = _workspace_display(workspace, owner_email=owner.email if owner else None)
        sync_state = _sync_state_for(workspace, has_snapshot=has_snapshot)
        health = _workspace_health(
            owner=owner,
            owner_state=_account_state(owner, owner_finding) if owner is not None else None,
            sync_state=sync_state,
            joined_member_count=counts["joined_member_count"],
            managed_count=len(managed),
            local_only=0,
            seat_limit=workspace.seat_limit,
            occupied_seats=getattr(workspace, "occupied_seats", None),
            status=workspace.status,
        )
        workspace_health.append(
            {
                "id": workspace.id,
                "name": display["display_name"],
                "display_name": display["display_name"],
                "official_workspace_id": workspace.official_workspace_id,
                "owner_email": owner.email if owner else None,
                "joined_people_total": counts["joined_people_total"],
                "joined_member_count": counts["joined_member_count"],
                "official_owner_count": counts.get("official_owner_count"),
                "official_member_count": counts.get("official_member_count"),
                "primary_mother_count": counts.get("primary_mother_count"),
                "managed_count": len(managed),
                "members": counts["joined_member_count"],
                "seat_limit": workspace.seat_limit,
                "occupied_seats": getattr(workspace, "occupied_seats", None),
                "health": health,
                "status": workspace.status,
                "sync_state": sync_state,
                "last_sync": isoformat(workspace.last_official_sync_at),
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
    accounts_by_email = {normalize_email(row.email): row for row in accounts}
    members_by_workspace: dict[int, list] = defaultdict(list)
    for row in memberships:
        members_by_workspace[row.workspace_id].append(row)
    snapshots = await identity_repo.list_official_snapshots(db)
    snapshots_by_workspace: dict[int, list] = defaultdict(list)
    for row in snapshots:
        snapshots_by_workspace[row.workspace_id].append(row)

    items = []
    for workspace in workspaces:
        owner = accounts_by_id.get(workspace.owner_account_id) if workspace.owner_account_id else None
        members = members_by_workspace.get(workspace.id, [])
        seated = [
            row
            for row in members
            if row.membership_state in {MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_INVITED}
            and not is_workspace_owner(workspace, row.account_id)
        ]
        snaps = snapshots_by_workspace.get(workspace.id, [])
        official_members = []
        official_by_email: dict[str, Any] = {}
        owner_email = normalize_email(owner.email) if owner is not None else ""
        for snap in snaps:
            email = normalize_email(snap.normalized_email)
            item = {
                "email": email,
                "name": snap.display_name,
                "role": normalize_official_role(snap.official_role),
                "user_id": snap.official_user_id,
                "seat_type": snap.seat_type,
                "state": snap.remote_state,
                "added_at": isoformat(snap.added_at),
                "is_owner": is_owner_role(snap.official_role),
            }
            official_members.append(item)
            official_by_email[email] = item
        counts = _official_counts(snaps, owner_email=owner_email) if snaps or workspace.last_official_sync_at else {
            "joined_people_total": None,
            "owner_count": None,
            "official_owner_count": None,
            "official_member_count": None,
            "primary_mother_count": None,
            "joined_member_count": None,
            "invited_count": None,
        }
        local_by_email: dict[str, dict[str, Any]] = {}
        member_items = []
        for row in seated:
            account = accounts_by_id.get(row.account_id)
            if account is None:
                continue
            payload = {
                "id": account.id,
                "email": account.email,
                "purpose": account.local_purpose,
                "official_role": row.official_role,
                "official_user_id": row.official_user_id,
                "user_id": row.official_user_id,
                "membership_state": row.membership_state,
                "auth": account.auth_state,
                "state": _account_state(account, findings_by_id.get(account.id)),
            }
            member_items.append(payload)
            local_by_email[normalize_email(account.email)] = payload
        member_items.sort(key=lambda item: ((item.get("email") or "").lower(), item.get("id") or 0))

        reconciliation = []
        matched = remote_only = local_only = pending_invites = 0
        emails = sorted(set(official_by_email) | set(local_by_email))
        for email in emails:
            remote = official_by_email.get(email)
            local = local_by_email.get(email)
            is_owner = bool(owner_email) and email == owner_email
            if is_owner:
                status = "owner"
            elif remote and local:
                status = "managed"
                matched += 1
            elif remote and not local:
                status = "invited" if remote.get("state") == "invited" else "remote_only"
                if status == "remote_only":
                    remote_only += 1
                else:
                    pending_invites += 1
            else:
                status = "local_only"
                local_only += 1
            candidate = accounts_by_email.get(email)
            reconciliation.append(
                {
                    "email": email,
                    "status": status,
                    "status_label": membership_status_label(status),
                    "name": (remote or {}).get("name"),
                    "role": (remote or {}).get("role") or (local or {}).get("official_role"),
                    "user_id": (remote or {}).get("user_id") or (local or {}).get("user_id"),
                    "official_user_id": (remote or {}).get("user_id") or (local or {}).get("official_user_id"),
                    "local_account_id": (local or {}).get("id"),
                    "candidate_account_id": candidate.id if candidate is not None and not local else None,
                    "is_owner": is_owner,
                    "actionable": status in {"remote_only", "local_only", "conflict"},
                    "note": {
                        "remote_only": "官方已加入，本地尚未接入。点接入会按该邮箱建立本地子号，完成授权后才能读额度。",
                        "local_only": "本地有账号记录，但官方成员列表未找到对应邮箱。",
                        "invited": "官方邀请仍待接受。",
                        "conflict": "身份冲突，需人工核对。",
                    }.get(status),
                }
            )
        actionable_items = [row for row in reconciliation if row.get("actionable")]
        has_snapshot = bool(snaps) or bool(workspace.last_official_sync_at)
        sync_state = _sync_state_for(workspace, has_snapshot=has_snapshot)
        owner_finding = findings_by_id.get(owner.id) if owner is not None else None
        display = _workspace_display(workspace, owner_email=owner.email if owner else None)
        health = _workspace_health(
            owner=owner,
            owner_state=_account_state(owner, owner_finding) if owner is not None else None,
            sync_state=sync_state,
            joined_member_count=counts["joined_member_count"],
            managed_count=matched,
            local_only=local_only,
            seat_limit=workspace.seat_limit,
            occupied_seats=getattr(workspace, "occupied_seats", None),
            status=workspace.status,
        )
        items.append(
            {
                "id": workspace.id,
                "name": display["display_name"],
                "display_name": display["display_name"],
                "official_name": display.get("official_name") or getattr(workspace, "official_name", None),
                "custom_name": display.get("custom_name") or getattr(workspace, "custom_name", None),
                "name_source": display.get("name_source") or getattr(workspace, "name_source", None),
                "official_name_synced_at": isoformat(getattr(workspace, "official_name_synced_at", None)),
                "official_name_last_error": display.get("official_name_last_error") or getattr(workspace, "official_name_last_error", None),
                "official_name_payload_source": display.get("official_name_payload_source") or getattr(workspace, "official_name_payload_source", None),
                "official_workspace_id": workspace.official_workspace_id,
                "owner_email": owner.email if owner else None,
                "owner_purpose": owner.local_purpose if owner else None,
                "owner_auth": owner.auth_state if owner else None,
                "owner_proxy": mask_proxy_url(owner.proxy) if owner and owner.proxy else None,
                "owner_proxy_set": bool(owner and owner.proxy),
                "proxy_profile_id": owner.proxy_profile_id if owner else None,
                # Backward-compatible fields. Prefer nested official/managed blocks.
                "members": counts["joined_member_count"] if has_snapshot else None,
                "managed_count": matched,
                "member_accounts": member_items,
                "official_members": official_members,
                "official": {
                    "sync_state": sync_state,
                    "synced_at": isoformat(workspace.last_official_sync_at),
                    "joined_people_total": counts["joined_people_total"] if has_snapshot else None,
                    "owner_count": counts["owner_count"] if has_snapshot else None,
                    "official_owner_count": counts.get("official_owner_count") if has_snapshot else None,
                    "official_member_count": counts.get("official_member_count") if has_snapshot else None,
                    "primary_mother_count": counts.get("primary_mother_count") if has_snapshot else None,
                    "joined_member_count": counts["joined_member_count"] if has_snapshot else None,
                    "invited_count": counts["invited_count"] if has_snapshot else None,
                    "occupied_seats": getattr(workspace, "occupied_seats", None),
                    "seat_limit": workspace.seat_limit,
                    "members": official_members,
                    # legacy aliases
                    "parsed_joined": counts["joined_people_total"] if has_snapshot else None,
                    "parsed_invited": counts["invited_count"] if has_snapshot else None,
                },
                "managed": {"count": matched, "accounts": member_items},
                "reconciliation": {
                    "managed": matched,
                    "matched": matched,
                    "official_unmanaged": remote_only,
                    "remote_only": remote_only,
                    "local_missing_official": local_only,
                    "local_only": local_only,
                    "pending_invites": pending_invites,
                    "actionable_count": len(actionable_items),
                    "items": reconciliation,
                    "actionable_items": actionable_items,
                },
                "seat_limit": workspace.seat_limit,
                "occupied_seats": getattr(workspace, "occupied_seats", None),
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
        workspace_names = []
        for row in active:
            workspace = workspaces_by_id.get(row.workspace_id)
            if workspace is None:
                continue
            owner = None
            if workspace.owner_account_id:
                # owner email only used to avoid email fallback naming
                owner = next((item for item in accounts if item.id == workspace.owner_account_id), None)
            workspace_names.append(
                _workspace_display(workspace, owner_email=owner.email if owner else None)["display_name"]
            )
        binding_rows = bindings_by_account.get(account.id, [])
        primary_membership = active[0] if active else None
        membership_payload = []
        for row in active:
            workspace = workspaces_by_id.get(row.workspace_id)
            owner = None
            if workspace and workspace.owner_account_id:
                owner = next((item for item in accounts if item.id == workspace.owner_account_id), None)
            membership_payload.append(
                {
                    "workspace_id": row.workspace_id,
                    "workspace": (
                        _workspace_display(workspace, owner_email=owner.email if owner else None)["display_name"]
                        if workspace is not None
                        else None
                    ),
                    "official_role": row.official_role,
                    "membership_state": row.membership_state,
                }
            )
        owned = [row for row in workspaces if row.owner_account_id == account.id]
        primary_workspace_id = (
            owned[0].id if owned else (primary_membership.workspace_id if primary_membership else (active[0].workspace_id if active else None))
        )
        primary_binding = binding_rows[0] if binding_rows else None
        from app.application.sub2api_publish import sub2api_publish_eligibility

        eligibility = sub2api_publish_eligibility(account)
        items.append(
            {
                "id": account.id,
                "email": account.email,
                "purpose": account.local_purpose,
                "official_plan": account.official_plan,
                "official_user_id": account.official_user_id,
                "official_account_id": account.official_account_id,
                "workspace": workspace_names[0] if workspace_names else None,
                "workspace_id": primary_workspace_id,
                "primary_workspace_id": primary_workspace_id,
                "memberships": membership_payload,
                "official_role": primary_membership.official_role if primary_membership else None,
                "membership_state": primary_membership.membership_state if primary_membership else None,
                "quota_7d": None,
                "auth": account.auth_state,
                "sub2api": _binding_label(binding_rows),
                "sub2api_publish": eligibility,
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
