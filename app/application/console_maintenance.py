"""Console maintenance actions: operation archive, workspace naming, remote-only link."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import ensure_membership
from app.application.operations import operation_store
from app.core.time import utcnow
from app.domain.automation import ACTIVE_STATES, TERMINAL_STATES
from app.domain.identity import LOCAL_PURPOSE_CHILD, MEMBERSHIP_STATE_JOINED
from app.domain.identity.ids import normalize_email
from app.domain.workspaces.names import apply_custom_name, apply_official_name, is_placeholder_or_email_name, resolve_display_name
from app.integrations.openai.member_adapter import is_owner_role
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot


async def archive_operation(db: AsyncSession, public_id: str, *, reason: str | None = None) -> dict[str, Any]:
    row = await operation_store.get_by_public_id(db, public_id)
    if row is None:
        return {"ok": False, "error": "operation not found", "error_code": "not_found"}
    if row.state in ACTIVE_STATES:
        return {"ok": False, "error": "active operations cannot be archived", "error_code": "not_terminal"}
    if row.state not in TERMINAL_STATES:
        return {"ok": False, "error": f"state={row.state} cannot be archived", "error_code": "not_terminal"}
    if row.archived_at:
        return {"ok": True, "operation_id": row.public_id, "archived": True, "already": True}
    row.archived_at = utcnow()
    row.archive_reason = (str(reason or "").strip() or "manual_clear")[:120]
    row.updated_at = utcnow()
    await db.commit()
    return {"ok": True, "operation_id": row.public_id, "archived": True}


async def restore_operation(db: AsyncSession, public_id: str) -> dict[str, Any]:
    row = await operation_store.get_by_public_id(db, public_id)
    if row is None:
        return {"ok": False, "error": "operation not found", "error_code": "not_found"}
    if not row.archived_at:
        return {"ok": True, "operation_id": row.public_id, "archived": False, "already": True}
    row.archived_at = None
    row.archive_reason = None
    row.updated_at = utcnow()
    await db.commit()
    return {"ok": True, "operation_id": row.public_id, "archived": False}


async def bulk_archive_operations(
    db: AsyncSession,
    public_ids: list[str],
    *,
    reason: str | None = None,
    only_terminal: bool = True,
) -> dict[str, Any]:
    archived: list[str] = []
    skipped: list[dict[str, str]] = []
    for public_id in public_ids or []:
        row = await operation_store.get_by_public_id(db, str(public_id))
        if row is None:
            skipped.append({"id": str(public_id), "reason": "not_found"})
            continue
        if row.archived_at:
            skipped.append({"id": str(public_id), "reason": "already_archived"})
            continue
        if only_terminal and row.state not in TERMINAL_STATES:
            skipped.append({"id": str(public_id), "reason": f"state={row.state}"})
            continue
        if row.state in ACTIVE_STATES:
            skipped.append({"id": str(public_id), "reason": "active"})
            continue
        row.archived_at = utcnow()
        row.archive_reason = (str(reason or "").strip() or "bulk_clear")[:120]
        row.updated_at = utcnow()
        archived.append(row.public_id)
    if archived:
        await db.commit()
    return {"ok": True, "archived": archived, "skipped": skipped, "count": len(archived)}


async def update_workspace_display_name(db: AsyncSession, workspace_id: int, custom_name: str | None) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    try:
        display = apply_custom_name(workspace, custom_name)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "error_code": "invalid_name"}
    workspace.updated_at = utcnow()
    await db.commit()
    return {"ok": True, "workspace_id": workspace.id, **display}


async def link_remote_only_member(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str,
    account_id: int | None = None,
) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    target = normalize_email(email)
    if not target:
        return {"ok": False, "error": "email required", "error_code": "email_required"}
    snap = (
        await db.execute(
            select(WorkspaceOfficialMemberSnapshot).where(
                WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id,
                WorkspaceOfficialMemberSnapshot.normalized_email == target,
            )
        )
    ).scalar_one_or_none()
    if snap is None:
        return {"ok": False, "error": "official member not found", "error_code": "not_found"}
    if is_owner_role(snap.official_role) or snap.remote_state == "invited":
        return {"ok": False, "error": "owner/invited cannot be linked this way", "error_code": "not_linkable"}
    if account_id is not None:
        account = await db.get(Account, int(account_id))
    else:
        account = (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none()
    if account is None:
        return {
            "ok": False,
            "error": "no local account with exact email; refuse creating empty credentials",
            "error_code": "no_local_account",
        }
    if normalize_email(account.email) != target:
        return {"ok": False, "error": "account email mismatch", "error_code": "email_mismatch"}
    await ensure_membership(
        db,
        workspace_id=workspace.id,
        account_id=account.id,
        official_role=snap.official_role or "member",
        membership_state=MEMBERSHIP_STATE_JOINED if snap.remote_state != "invited" else snap.remote_state,
        local_purpose=account.local_purpose or LOCAL_PURPOSE_CHILD,
        joined_at=snap.added_at or utcnow(),
    )
    membership = (
        await db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace.id,
                WorkspaceMembership.account_id == account.id,
            )
        )
    ).scalar_one_or_none()
    await db.commit()
    return {
        "ok": True,
        "workspace_id": workspace.id,
        "account_id": account.id,
        "email": account.email,
        "status": "managed",
        "membership_id": membership.id if membership else None,
        "message": f"已关联 {account.email}，状态变为已纳入本地管理",
    }


async def repair_workspace_names(db: AsyncSession) -> dict[str, Any]:
    rows = list((await db.execute(select(Workspace))).scalars())
    accounts = {row.id: row for row in (await db.execute(select(Account))).scalars()}
    changed = 0
    skipped = 0
    for workspace in rows:
        owner = accounts.get(workspace.owner_account_id) if workspace.owner_account_id else None
        owner_email = owner.email if owner else None
        custom = str(getattr(workspace, "custom_name", None) or "").strip()
        if custom:
            skipped += 1
            continue
        current = str(workspace.name or "").strip()
        official = getattr(workspace, "official_name", None)
        if not is_placeholder_or_email_name(current, owner_email=owner_email) and not is_placeholder_or_email_name(
            official, owner_email=owner_email
        ):
            if not getattr(workspace, "name_source", None):
                workspace.name_source = "legacy"
            skipped += 1
            continue
        before = current
        apply_official_name(workspace, official, owner_email=owner_email)
        display = resolve_display_name(workspace, owner_email=owner_email)
        if display["display_name"] != before:
            changed += 1
        else:
            skipped += 1
        workspace.updated_at = utcnow()
    if changed:
        await db.commit()
    return {"ok": True, "changed": changed, "skipped": skipped}
