"""Console maintenance actions: operation archive, workspace naming, remote-only link."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import ensure_membership, upsert_child_account
from app.application.operations import operation_store
from app.core.time import utcnow
from app.domain.automation import ACTIVE_STATES, TERMINAL_STATES, WORKSPACE_LOCK_ACTIONS
from app.domain.identity import LOCAL_PURPOSE_CHILD, LOCAL_PURPOSE_MOTHER, MEMBERSHIP_STATE_INVITED, MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_REMOVED
from app.domain.identity.ids import normalize_email
from app.domain.identity.policy import is_workspace_owner
from app.integrations.openai.member_adapter import normalize_official_role, official_roles_equivalent, parse_invite_role
from app.domain.workspaces.names import apply_custom_name, apply_official_name, is_placeholder_or_email_name, resolve_display_name
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot
from app.persistence.models.quota import QuotaSnapshot
from app.persistence.models.resources import HmeAliasLease
from app.persistence.models.vacancy import SeatVacancyEvent


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
        official_role=normalize_official_role(snap.official_role) or "member",
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
    workspaces=None,
    job_id: str | None = None,
    role: str = "owner",
) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    target = normalize_email(email)
    if not target or "@" not in target:
        return {"ok": False, "error": "email required", "error_code": "email_required"}
    owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
    if owner is not None and normalize_email(owner.email) == target:
        return {"ok": False, "error": "workspace owner cannot be invited as a child", "error_code": "not_linkable"}
    busy = await operation_store.active_for_workspace(
        db,
        workspace.id,
        actions=WORKSPACE_LOCK_ACTIONS,
        exclude_public_id=job_id,
    )
    if busy is not None:
        return {
            "ok": False,
            "error": f"Workspace {workspace.id} 已有 {busy.op_type} 任务 {busy.public_id} 在跑，避免两边同时踢拉",
            "error_code": "operation_conflict",
            "operation_id": busy.public_id,
        }
    from app.application.workspaces import workspace_service as default_workspaces

    service = workspaces or default_workspaces
    live, live_item = await service.lookup_live_member(db, workspace, target)
    already_joined = bool(live_item and live_item.get("status") == "joined")
    already_invited = bool(live_item and live_item.get("status") == "invited")
    invited_now = False
    requested_role = parse_invite_role(role)
    live_role = normalize_official_role((live_item or {}).get("role"))
    if already_joined and live_role not in {"unknown", ""} and not official_roles_equivalent(live_role, requested_role):
        return {
            "ok": False,
            "error": f"{target} 已经加入，官方角色是 {live_role}，本次邀请不会改成 {requested_role}",
            "error_code": "already_joined_role_mismatch",
            "existing_role": live_role,
            "requested_role": requested_role,
        }
    if already_invited and live_role not in {"unknown", ""} and not official_roles_equivalent(live_role, requested_role):
        return {
            "ok": False,
            "error": f"{target} 已有官方邀请，但角色是 {live_role}，与本次请求的 {requested_role} 不一致",
            "error_code": "invite_role_mismatch",
            "existing_role": live_role,
            "requested_role": requested_role,
        }
    if not already_joined and not already_invited:
        invite = await service.invite_member(db, workspace.id, target, role=requested_role)
        if not invite.get("success"):
            return {
                "ok": False,
                "error": invite.get("error") or "邀请失败",
                "error_code": invite.get("error_code") or "invite_failed",
            }
        invited_now = True

    account, created = await upsert_child_account(db, email=target, status="active" if already_joined else "invited")
    if is_workspace_owner(workspace, account.id):
        return {"ok": False, "error": "workspace owner cannot be invited as a child", "error_code": "not_linkable"}
    needs_auth = not bool((account.access_token_encrypted or "").strip())
    if created or (needs_auth and str(account.auth_state or "") in {"", "unknown"}):
        account.auth_state = "oauth_required"
    if created and workspace.source_team_id and not account.source_team_id:
        account.source_team_id = workspace.source_team_id
    membership_state = MEMBERSHIP_STATE_JOINED if already_joined else MEMBERSHIP_STATE_INVITED
    await ensure_membership(
        db,
        workspace_id=workspace.id,
        account_id=account.id,
        official_role=live_role if live_role not in {"unknown", ""} else requested_role,
        membership_state=membership_state,
        local_purpose=LOCAL_PURPOSE_CHILD,
        joined_at=utcnow() if already_joined else None,
    )
    snap = (
        await db.execute(
            select(WorkspaceOfficialMemberSnapshot).where(
                WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id,
                WorkspaceOfficialMemberSnapshot.normalized_email == target,
            )
        )
    ).scalar_one_or_none()
    remote_state = "joined" if already_joined else "invited"
    if snap is None:
        db.add(
            WorkspaceOfficialMemberSnapshot(
                workspace_id=workspace.id,
                normalized_email=target,
                official_role=live_role if live_role not in {"unknown", ""} else requested_role,
                official_user_id=(live_item or {}).get("user_id"),
                remote_state=remote_state,
                fetched_at=utcnow(),
            )
        )
    else:
        snap.remote_state = remote_state
        snap.official_role = live_role if live_role not in {"unknown", ""} else requested_role
        snap.official_user_id = (live_item or {}).get("user_id") or snap.official_user_id
        snap.fetched_at = utcnow()
        snap.updated_at = utcnow()
    membership = (
        await db.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace.id,
                WorkspaceMembership.account_id == account.id,
            )
        )
    ).scalar_one_or_none()
    await db.commit()
    if already_joined:
        message = f"{account.email} 已在官方席位，已接入本地" + ("。点「授权」，用这个邮箱登录后再读额度" if needs_auth else "")
        status = "managed"
    elif already_invited and not invited_now:
        message = f"{account.email} 的官方邀请已存在" + ("。点「授权」，用这个邮箱登录后再读额度" if needs_auth else "")
        status = "invited"
    else:
        message = f"已邀请 {account.email} 进入官方席位" + ("。点「授权」，用这个邮箱登录后再读额度" if needs_auth else "")
        status = "invited"
    return {
        "ok": True,
        "workspace_id": workspace.id,
        "account_id": account.id,
        "email": account.email,
        "created": created,
        "needs_auth": needs_auth,
        "status": status,
        "invited": status == "invited",
        "already_joined": already_joined,
        "already_invited": already_invited and not invited_now,
        "membership_id": membership.id if membership else None,
        "membership_state": membership_state,
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


async def purge_local_child_record(db: AsyncSession, workspace: Workspace, account: Account) -> dict[str, Any]:
    if is_workspace_owner(workspace, account.id) or account.local_purpose == LOCAL_PURPOSE_MOTHER:
        return {"ok": False, "error": "workspace owner cannot be permanently deleted", "error_code": "not_linkable"}
    owned = (await db.execute(select(Workspace.id).where(Workspace.owner_account_id == account.id))).scalars().all()
    if owned:
        return {"ok": False, "error": "account owns a workspace and cannot be permanently deleted", "error_code": "not_linkable"}
    email = normalize_email(account.email)
    account_id = account.id
    from app.persistence.models.operations import Operation

    await db.execute(delete(QuotaSnapshot).where(QuotaSnapshot.account_id == account_id))
    await db.execute(delete(ExternalBinding).where(ExternalBinding.local_account_id == account_id))
    await db.execute(delete(WorkspaceMembership).where(WorkspaceMembership.account_id == account_id))
    if email:
        await db.execute(
            delete(WorkspaceOfficialMemberSnapshot).where(
                WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id,
                WorkspaceOfficialMemberSnapshot.normalized_email == email,
            )
        )
        await db.execute(delete(HmeAliasLease).where(HmeAliasLease.email == email))
    await db.execute(update(Account).where(Account.source_child_account_id == account_id).values(source_child_account_id=None))
    await db.execute(update(Operation).where(Operation.account_id == account_id).values(account_id=None))
    await db.delete(account)
    await db.flush()
    return {"ok": True, "purged_account_id": account_id, "email": email}


async def update_workspace_member_role(
    db: AsyncSession,
    workspace_id: int,
    *,
    email: str,
    role: str = "owner",
    user_id: str | None = None,
    workspaces=None,
) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    target = normalize_email(email)
    if not target or "@" not in target:
        return {"ok": False, "error": "email required", "error_code": "email_required"}
    try:
        requested_role = parse_invite_role(role)
    except ValueError:
        return {"ok": False, "error": "官方角色只能是 Owner 或 Member", "error_code": "invalid_invite_role"}
    from app.application.workspaces import workspace_service as default_workspaces

    service = workspaces or default_workspaces
    result = await service.update_member_role(db, workspace.id, target, requested_role, user_id=user_id)
    if not result.get("success"):
        return {
            "ok": False,
            "error": result.get("error") or "改官方角色失败",
            "error_code": result.get("error_code") or "role_update_failed",
            "existing_role": result.get("existing_role"),
            "requested_role": requested_role,
        }
    account = (await db.execute(select(Account).where(Account.email == target))).scalar_one_or_none()
    if account is not None:
        membership = (
            await db.execute(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace.id,
                    WorkspaceMembership.account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        if membership is not None:
            membership.official_role = requested_role
            membership.updated_at = utcnow()
    snap = (
        await db.execute(
            select(WorkspaceOfficialMemberSnapshot).where(
                WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id,
                WorkspaceOfficialMemberSnapshot.normalized_email == target,
            )
        )
    ).scalar_one_or_none()
    if snap is not None:
        snap.official_role = requested_role
        snap.updated_at = utcnow()
    await db.commit()
    payload = dict(result)
    payload["ok"] = True
    payload["workspace_id"] = workspace.id
    payload["role"] = requested_role
    if account is not None:
        payload["account_id"] = account.id
        payload["local_purpose"] = account.local_purpose
    return payload


async def delete_local_workspace(db: AsyncSession, workspace_id: int) -> dict[str, Any]:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    busy = await operation_store.active_for_workspace(
        db,
        workspace.id,
        actions=WORKSPACE_LOCK_ACTIONS,
    )
    if busy is not None:
        return {
            "ok": False,
            "error": f"Workspace {workspace.id} 已有 {busy.op_type} 任务 {busy.public_id} 在跑，先等它结束再删",
            "error_code": "operation_conflict",
            "operation_id": busy.public_id,
        }
    from app.persistence.models.operations import Operation

    memberships = list(
        (await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace.id))).scalars()
    )
    child_ids = [row.account_id for row in memberships if not is_workspace_owner(workspace, row.account_id)]
    owner_id = workspace.owner_account_id
    resolved = resolve_display_name(workspace)
    display = resolved.get("display_name") or workspace.name or f"Workspace #{workspace.id}"
    purged_children = 0
    kept_children = 0
    for account_id in child_ids:
        child = await db.get(Account, account_id)
        if child is None:
            continue
        other_memberships = list(
            (
                await db.execute(
                    select(WorkspaceMembership.id).where(
                        WorkspaceMembership.account_id == account_id,
                        WorkspaceMembership.workspace_id != workspace.id,
                    )
                )
            ).scalars()
        )
        other_owned = list(
            (
                await db.execute(
                    select(Workspace.id).where(
                        Workspace.owner_account_id == account_id,
                        Workspace.id != workspace.id,
                    )
                )
            ).scalars()
        )
        if other_memberships or other_owned:
            await db.execute(
                delete(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == workspace.id,
                    WorkspaceMembership.account_id == account_id,
                )
            )
            await db.execute(
                delete(ExternalBinding).where(
                    ExternalBinding.workspace_id == workspace.id,
                    ExternalBinding.local_account_id == account_id,
                )
            )
            await db.execute(
                delete(QuotaSnapshot).where(
                    QuotaSnapshot.workspace_id == workspace.id,
                    QuotaSnapshot.account_id == account_id,
                )
            )
            kept_children += 1
            continue
        result = await purge_local_child_record(db, workspace, child)
        if result.get("ok"):
            purged_children += 1
    await db.execute(delete(WorkspaceOfficialMemberSnapshot).where(WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id))
    await db.execute(delete(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace.id))
    await db.execute(delete(ExternalBinding).where(ExternalBinding.workspace_id == workspace.id))
    await db.execute(delete(QuotaSnapshot).where(QuotaSnapshot.workspace_id == workspace.id))
    await db.execute(delete(SeatVacancyEvent).where(SeatVacancyEvent.workspace_id == workspace.id))
    await db.execute(update(HmeAliasLease).where(HmeAliasLease.workspace_id == workspace.id).values(workspace_id=None))
    await db.execute(update(Operation).where(Operation.workspace_id == workspace.id).values(workspace_id=None))
    await db.delete(workspace)
    await db.flush()
    if owner_id:
        still_owns = (
            await db.execute(select(Workspace.id).where(Workspace.owner_account_id == owner_id))
        ).scalars().all()
        still_member = (
            await db.execute(select(WorkspaceMembership.id).where(WorkspaceMembership.account_id == owner_id))
        ).scalars().all()
        if not still_owns and not still_member:
            owner = await db.get(Account, owner_id)
            if owner is not None and owner.local_purpose == LOCAL_PURPOSE_MOTHER:
                await db.execute(delete(QuotaSnapshot).where(QuotaSnapshot.account_id == owner_id))
                await db.execute(delete(ExternalBinding).where(ExternalBinding.local_account_id == owner_id))
                await db.execute(update(Operation).where(Operation.account_id == owner_id).values(account_id=None))
                await db.delete(owner)
                await db.flush()
    await db.commit()
    return {
        "ok": True,
        "workspace_id": workspace_id,
        "purged_children": purged_children,
        "kept_children": kept_children,
        "status": "deleted",
        "message": f"已从本地删除 {display}，未改官方 Team",
    }
