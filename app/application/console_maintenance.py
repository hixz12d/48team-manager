"""Console maintenance actions: operation archive, workspace naming, remote-only link."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import ensure_membership, upsert_child_account
from app.application.operations import operation_store
from app.core.time import utcnow
from app.domain.automation import ACTIVE_STATES, TERMINAL_STATES
from app.domain.identity import LOCAL_PURPOSE_CHILD, MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_REMOVED
from app.domain.identity.ids import normalize_email
from app.domain.identity.policy import is_workspace_owner
from app.domain.workspaces.names import apply_custom_name, apply_official_name, is_placeholder_or_email_name, resolve_display_name
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


async def sync_workspace_official_name(db: AsyncSession, workspace_id: int) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
    from app.application.workspace_metadata import workspace_metadata_resolver

    result = await workspace_metadata_resolver.refresh(db, workspace, owner, persist=True)
    result["workspace_id"] = workspace.id
    return result


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
    if snap.remote_state == "invited":
        return {"ok": False, "error": "invited members cannot be linked until they join", "error_code": "not_linkable"}
    created = False
    if account_id is not None:
        account = await db.get(Account, int(account_id))
        if account is None:
            return {"ok": False, "error": "account not found", "error_code": "not_found"}
    else:
        account = (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none()
    if account is None:
        account, created = await upsert_child_account(db, email=target, status="active")
    if normalize_email(account.email) != target:
        return {"ok": False, "error": "account email mismatch", "error_code": "email_mismatch"}
    owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
    if is_workspace_owner(workspace, account.id) or (owner is not None and normalize_email(owner.email) == target):
        return {"ok": False, "error": "workspace owner cannot be linked as a child", "error_code": "not_linkable"}
    needs_auth = not bool((account.access_token_encrypted or "").strip())
    if created or (needs_auth and str(account.auth_state or "") in {"", "unknown"}):
        account.auth_state = "oauth_required"
    if created and workspace.source_team_id and not account.source_team_id:
        account.source_team_id = workspace.source_team_id
    await ensure_membership(
        db,
        workspace_id=workspace.id,
        account_id=account.id,
        official_role=snap.official_role or "member",
        membership_state=MEMBERSHIP_STATE_JOINED,
        local_purpose=LOCAL_PURPOSE_CHILD,
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
    message = f"已按官方邮箱接入 {account.email}" if created else f"已接入 {account.email}"
    if needs_auth:
        message += "，请完成授权后才能读取额度"
    return {
        "ok": True,
        "workspace_id": workspace.id,
        "account_id": account.id,
        "email": account.email,
        "created": created,
        "needs_auth": needs_auth,
        "status": "managed",
        "membership_id": membership.id if membership else None,
        "message": message,
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


async def add_local_child(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str,
) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    target = normalize_email(email)
    if not target or "@" not in target:
        return {"ok": False, "error": "email required", "error_code": "email_required"}
    owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
    if owner is not None and normalize_email(owner.email) == target:
        return {"ok": False, "error": "workspace owner cannot be added as a child", "error_code": "not_linkable"}
    account, created = await upsert_child_account(db, email=target, status="active")
    if is_workspace_owner(workspace, account.id):
        return {"ok": False, "error": "workspace owner cannot be added as a child", "error_code": "not_linkable"}
    needs_auth = not bool((account.access_token_encrypted or "").strip())
    if created or (needs_auth and str(account.auth_state or "") in {"", "unknown"}):
        account.auth_state = "oauth_required"
    if created and workspace.source_team_id and not account.source_team_id:
        account.source_team_id = workspace.source_team_id
    await ensure_membership(
        db,
        workspace_id=workspace.id,
        account_id=account.id,
        official_role="member",
        membership_state=MEMBERSHIP_STATE_JOINED,
        local_purpose=LOCAL_PURPOSE_CHILD,
        joined_at=utcnow(),
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
    message = f"已加入本地子号 {account.email}" if created else f"已接入本地子号 {account.email}"
    if needs_auth:
        message += "，请完成授权后才能读取额度"
    return {
        "ok": True,
        "workspace_id": workspace.id,
        "account_id": account.id,
        "email": account.email,
        "created": created,
        "needs_auth": needs_auth,
        "status": "managed",
        "membership_id": membership.id if membership else None,
        "message": message,
    }


async def remove_local_child(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str | None = None,
    account_id: int | None = None,
) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    account = None
    if account_id is not None:
        account = await db.get(Account, int(account_id))
    target = normalize_email(email or "")
    if account is None and target:
        account = (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none()
    if account is None:
        return {"ok": False, "error": "account not found", "error_code": "not_found"}
    if is_workspace_owner(workspace, account.id):
        return {"ok": False, "error": "workspace owner cannot be removed as a child", "error_code": "not_linkable"}
    membership = (
        await db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace.id,
                WorkspaceMembership.account_id == account.id,
            )
        )
    ).scalar_one_or_none()
    if membership is None:
        return {"ok": False, "error": "membership not found", "error_code": "not_found"}
    if membership.membership_state == MEMBERSHIP_STATE_REMOVED:
        return {
            "ok": True,
            "already": True,
            "workspace_id": workspace.id,
            "account_id": account.id,
            "email": account.email,
            "status": "removed",
            "message": f"{account.email} 已不在本地子号",
        }
    membership.membership_state = MEMBERSHIP_STATE_REMOVED
    membership.removed_at = utcnow()
    await db.commit()
    return {
        "ok": True,
        "workspace_id": workspace.id,
        "account_id": account.id,
        "email": account.email,
        "status": "removed",
        "message": f"已从本地子号移除 {account.email}，未改官方成员",
    }
