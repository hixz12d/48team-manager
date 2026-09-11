"""Official Workspace member sync. Never invents local credential accounts."""

from __future__ import annotations

import logging

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.operations import operation_store
from app.application.workspaces import workspace_service
from app.core.time import utcnow
from app.domain.identity import MEMBERSHIP_STATE_INVITED, MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_REMOVED
from app.domain.identity.ids import normalize_email
from app.integrations.openai.member_adapter import (
    adapt_collection,
    is_admin_role,
    is_owner_role,
    normalize_official_role,
    validate_fetch_counts,
)
from app.persistence.models.identity import Account, WorkspaceMembership, WorkspaceOfficialMemberSnapshot


logger = logging.getLogger(__name__)

def _counts_from_fetch(payload: dict[str, Any], *, default_state: str) -> dict[str, Any]:
    items = payload.get("members") if default_state == "joined" else payload.get("items")
    if items is None:
        items = payload.get("members") or payload.get("items") or []
    adapted = adapt_collection(items, default_state=default_state)
    reported = payload.get("reported_total")
    if "reported_total" not in payload:
        reported = payload.get("total")
    try:
        reported_total = int(reported) if reported is not None else None
    except (TypeError, ValueError):
        reported_total = None
    check = validate_fetch_counts(
        reported_total=reported_total,
        raw_item_count=int(payload.get("raw_item_count") or adapted["raw_item_count"]),
        parsed_item_count=adapted["parsed_item_count"],
        invalid_item_count=adapted["invalid_item_count"],
        incomplete=bool(payload.get("incomplete")),
    )
    if not payload.get("success"):
        check = {
            "ok": False,
            "error_code": payload.get("error_code") or "fetch_failed",
            "error": payload.get("error") or "failed to fetch official members",
        }
    return {
        **adapted,
        "reported_total": reported_total,
        "seat_metadata": payload.get("seat_metadata") or {},
        "check": check,
        "error": payload.get("error"),
        "error_code": payload.get("error_code"),
        "success": bool(payload.get("success")) and bool(check.get("ok")),
    }


class WorkspaceSyncService:
    def __init__(self, workspaces=None):
        self.workspaces = workspaces or workspace_service

    def _owner_emails(self, workspace, owner: Account | None) -> set[str]:
        emails = set()
        if owner is not None:
            emails.add(normalize_email(owner.email))
        return emails

    def _merge_remote_rows(self, members: dict[str, Any], invites: dict[str, Any]) -> dict[str, dict[str, Any]]:
        remote_rows: dict[str, dict[str, Any]] = {}
        for item in members.get("members") or []:
            email = item["email"]
            remote_rows[email] = {**item, "state": "joined"}
        for item in invites.get("members") or []:
            email = item["email"]
            existing = remote_rows.get(email)
            if existing and existing.get("state") == "joined":
                continue
            remote_rows[email] = {**item, "state": "invited"}
        return remote_rows

    def _reconciliation(
        self,
        *,
        workspace,
        owner: Account | None,
        remote_rows: dict[str, dict[str, Any]],
        local_by_email: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        owner_emails = self._owner_emails(workspace, owner)
        items = []
        matched = 0
        remote_only = 0
        local_only = 0
        invited = 0
        emails = sorted(set(remote_rows) | set(local_by_email))
        for email in emails:
            remote = remote_rows.get(email)
            local = local_by_email.get(email)
            is_owner = email in owner_emails
            if is_owner:
                status = "owner"
            elif remote and local:
                status = "managed"
                matched += 1
            elif remote and not local:
                if remote.get("state") == "invited":
                    status = "invited"
                    invited += 1
                else:
                    status = "remote_only"
                    remote_only += 1
            else:
                status = "local_only"
                local_only += 1
            items.append(
                {
                    "email": email,
                    "status": status,
                    "diff": status,
                    "remote_state": (remote or {}).get("state"),
                    "official_role": (remote or {}).get("role") or (local or {}).get("official_role"),
                    "name": (remote or {}).get("name"),
                    "local_account_id": (local or {}).get("account_id"),
                    "local_membership_state": (local or {}).get("membership_state"),
                    "is_owner": is_owner,
                }
            )
        return {
            "matched": matched,
            "managed": matched,
            "remote_only": remote_only,
            "local_only": local_only,
            "invited": invited,
            "items": items,
        }

    async def _local_by_email(self, db: AsyncSession, workspace_id: int) -> dict[str, dict[str, Any]]:
        local_accounts = {account.id: account for account in (await db.execute(select(Account))).scalars()}
        local_memberships = list(
            (await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace_id))).scalars()
        )
        local_by_email: dict[str, dict[str, Any]] = {}
        for membership in local_memberships:
            account = local_accounts.get(membership.account_id)
            if account is None:
                continue
            local_by_email[normalize_email(account.email)] = {
                "account_id": account.id,
                "email": account.email,
                "membership_state": membership.membership_state,
                "official_role": membership.official_role,
                "local_purpose": membership.local_purpose or account.local_purpose,
            }
        return local_by_email

    async def sync_workspace(self, db: AsyncSession, workspace_id: int, *, operation=None) -> dict[str, Any]:
        workspace = await self.workspaces.load_workspace(db, workspace_id)
        if workspace is None:
            return {"success": False, "error": "workspace not found", "error_code": "not_found"}

        owner = await self.workspaces.owner_account(db, workspace)
        queued_execution = operation is not None
        operation = operation or await operation_store.create(
            db,
            op_type="workspace_sync",
            workspace_id=workspace.id,
            account_id=workspace.owner_account_id,
            email=(owner.email if owner else "") or "",
            input_payload={"workspace_id": workspace.id},
            source="manual",
        )
        await operation_store.note(db, operation, "fetch_members", "fetching official members")
        if queued_execution:
            await db.commit()
            members_raw = await self._read_only_collection(db, workspace, owner, "members")
        else:
            members_raw = await self.workspaces.get_members(db, workspace)
        members = _counts_from_fetch(members_raw, default_state="joined")
        if not members["success"]:
            result = {
                "success": False,
                "ok": False,
                "error": members["check"].get("error") or members.get("error") or "failed to fetch members",
                "error_code": members["check"].get("error_code") or members.get("error_code") or "members_failed",
                "status": "failed",
                "reported_total": members.get("reported_total"),
                "raw_item_count": members.get("raw_item_count"),
                "parsed_item_count": members.get("parsed_item_count"),
            }
            await operation_store.mark_step(
                db,
                operation,
                "fetch_members",
                state="failed",
                error_code=result["error_code"],
                error_message=result["error"],
                result={
                    "reported_total": members.get("reported_total"),
                    "raw_item_count": members.get("raw_item_count"),
                    "parsed_item_count": members.get("parsed_item_count"),
                    "invalid_item_count": members.get("invalid_item_count"),
                },
            )
            workspace.last_official_sync_state = "failed"
            workspace.updated_at = __import__('app.core.time', fromlist=['utcnow']).utcnow()
            if hasattr(workspace, 'last_official_sync_state'):
                workspace.last_official_sync_state = "failed"
            await operation_store.finish(db, operation, result)
            await db.commit()
            return {"ok": False, "operation_id": operation.public_id, **result}

        await operation_store.mark_step(
            db,
            operation,
            "fetch_members",
            state="success",
            result={
                "reported_total": members.get("reported_total"),
                "raw_item_count": members.get("raw_item_count"),
                "parsed_item_count": members.get("parsed_item_count"),
                "invalid_item_count": members.get("invalid_item_count"),
            },
        )
        await operation_store.note(db, operation, "fetch_invites", "fetching official invites")
        if queued_execution:
            await db.commit()
            invites_raw = await self._read_only_collection(db, workspace, owner, "invites")
        else:
            invites_raw = await self.workspaces.get_invites(db, workspace)
        invites = _counts_from_fetch(invites_raw, default_state="invited")
        if not invites["success"]:
            result = {
                "success": False,
                "ok": False,
                "error": invites["check"].get("error") or invites.get("error") or "failed to fetch invites",
                "error_code": invites["check"].get("error_code") or invites.get("error_code") or "invites_failed",
                "status": "failed",
                "reported_total": invites.get("reported_total"),
                "raw_item_count": invites.get("raw_item_count"),
                "parsed_item_count": invites.get("parsed_item_count"),
            }
            await operation_store.mark_step(
                db,
                operation,
                "fetch_invites",
                state="failed",
                error_code=result["error_code"],
                error_message=result["error"],
                result={
                    "reported_total": invites.get("reported_total"),
                    "raw_item_count": invites.get("raw_item_count"),
                    "parsed_item_count": invites.get("parsed_item_count"),
                    "invalid_item_count": invites.get("invalid_item_count"),
                },
            )
            if hasattr(workspace, 'last_official_sync_state'):
                workspace.last_official_sync_state = "failed"
            await operation_store.finish(db, operation, result)
            await db.commit()
            return {"ok": False, "operation_id": operation.public_id, **result}

        await operation_store.mark_step(
            db,
            operation,
            "fetch_invites",
            state="success",
            result={
                "reported_total": invites.get("reported_total"),
                "raw_item_count": invites.get("raw_item_count"),
                "parsed_item_count": invites.get("parsed_item_count"),
                "invalid_item_count": invites.get("invalid_item_count"),
            },
        )

        name_warning = None
        try:
            from app.application.workspace_metadata import workspace_metadata_resolver

            extra = [
                ("members", members_raw),
                ("members_seat_metadata", members_raw.get("seat_metadata") or {}),
                ("invites", invites_raw),
                ("invites_seat_metadata", invites_raw.get("seat_metadata") or {}),
            ]
            # Release operation-log writes before the read-only metadata request.
            if queued_execution:
                await db.commit()
            name_result = await workspace_metadata_resolver.refresh(db, workspace, owner, extra=extra, persist=False)
            if name_result.get("found") is False or not name_result.get("ok", True):
                name_warning = "Team 名称获取失败，已保留现有名称"
        except Exception:
            logger.exception("official name refresh failed workspace=%s", workspace.id)
            name_warning = "Team 名称获取失败，已保留现有名称"
        if name_warning:
            workspace.official_name_last_error = name_warning

        if queued_execution:
            await db.refresh(operation, ["cancel_requested"])
            if operation.cancel_requested:
                result = {"success": False, "status": "cancelled", "error_code": "cancelled", "error": "Sync cancelled before snapshot update"}
                await operation_store.finish(db, operation, result)
                await db.commit()
                return {"ok": False, "operation_id": operation.public_id, **result}
        stamp = utcnow()
        # Absence is evidence only when both collections parsed completely.
        can_reconcile_absence = all(
            collection["invalid_item_count"] == 0
            and collection["reported_total"] is not None
            and collection["reported_total"] == collection["parsed_item_count"]
            for collection in (members, invites)
        )
        removed_memberships = 0
        remote_rows = self._merge_remote_rows(members, invites)

        await db.execute(delete(WorkspaceOfficialMemberSnapshot).where(WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id))
        for row in remote_rows.values():
            db.add(
                WorkspaceOfficialMemberSnapshot(
                    workspace_id=workspace.id,
                    normalized_email=row["email"],
                    official_user_id=row.get("user_id"),
                    official_role=normalize_official_role(row.get("role")),
                    remote_state=row.get("state") or "joined",
                    display_name=row.get("name"),
                    seat_type=row.get("seat_type"),
                    added_at=row.get("added_at"),
                    fetched_at=stamp,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )
        for membership in (
            await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace.id))
        ).scalars():
            account = await db.get(Account, membership.account_id)
            if account is None:
                continue
            remote = remote_rows.get(normalize_email(account.email))
            if remote is None:
                if (
                    can_reconcile_absence
                    and account.id != workspace.owner_account_id
                    and membership.membership_state in {MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_INVITED}
                ):
                    membership.membership_state = MEMBERSHIP_STATE_REMOVED
                    membership.removed_at = stamp
                    removed_memberships += 1
                continue
            membership.official_role = normalize_official_role(remote.get("role")) or membership.official_role
            membership.official_user_id = remote.get("user_id") or membership.official_user_id
            remote_state = remote.get("state") or "joined"
            if membership.membership_state == MEMBERSHIP_STATE_REMOVED:
                continue
            if remote_state == "joined":
                membership.membership_state = MEMBERSHIP_STATE_JOINED
                membership.joined_at = membership.joined_at or stamp
                membership.removed_at = None
            elif remote_state == "invited":
                membership.membership_state = MEMBERSHIP_STATE_INVITED
                membership.removed_at = None

        await db.flush()
        reconciliation = self._reconciliation(
            workspace=workspace,
            owner=owner,
            remote_rows=remote_rows,
            local_by_email={
                email: row for email, row in (await self._local_by_email(db, workspace.id)).items()
                if row["membership_state"] in {MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_INVITED}
            },
        )

        seat_meta = dict(members.get("seat_metadata") or {})
        invite_meta = dict(invites.get("seat_metadata") or {})
        seat_limit = seat_meta.get("seat_limit") if seat_meta.get("seat_limit") is not None else invite_meta.get("seat_limit")
        occupied_seats = seat_meta.get("occupied_seats") if seat_meta.get("occupied_seats") is not None else invite_meta.get("occupied_seats")
        if seat_limit is not None:
            workspace.seat_limit = seat_limit
        if occupied_seats is not None:
            workspace.occupied_seats = occupied_seats
        workspace.last_official_sync_at = stamp
        workspace.last_official_sync_state = "fresh"
        workspace.updated_at = stamp
        workspace.version = int(workspace.version or 1) + 1

        joined_people_total = sum(1 for row in remote_rows.values() if row.get("state") == "joined")
        official_owner_count = sum(
            1 for row in remote_rows.values() if row.get("state") == "joined" and is_owner_role(row.get("role"))
        )
        official_admin_count = sum(
            1 for row in remote_rows.values() if row.get("state") == "joined" and is_admin_role(row.get("role"))
        )
        official_member_count = max(0, joined_people_total - official_owner_count - official_admin_count)
        primary_mother_count = 1 if owner else 0
        owner_count = official_owner_count or (1 if owner else 0)
        joined_member_count = official_member_count
        invited = sum(1 for row in remote_rows.values() if row.get("state") == "invited")
        warnings = [name_warning] if name_warning else []
        result = {
            "success": True,
            "ok": True,
            "status": "partial" if name_warning else "success",
            "outcome": "snapshot_updated",
            "warnings": warnings,
            "message": (
                f"同步完成：官方已加入 {joined_people_total} 人（Owner {official_owner_count} / Member {official_member_count}）；"
                f"本地 {primary_mother_count} 母号 / {reconciliation['managed']} 子号，"
                f"待邀请 {invited}；官方未接入 {reconciliation['remote_only']}"
                + (f"；{name_warning}" if name_warning else "")
            ),
            "workspace_id": workspace.id,
            "reported_total": members.get("reported_total"),
            "raw_item_count": int(members.get("raw_item_count") or 0) + int(invites.get("raw_item_count") or 0),
            "parsed_item_count": len(remote_rows),
            "invalid_item_count": int(members.get("invalid_item_count") or 0) + int(invites.get("invalid_item_count") or 0),
            "joined": joined_people_total,
            "joined_people_total": joined_people_total,
            "owner_count": owner_count,
            "official_owner_count": official_owner_count,
            "official_member_count": official_member_count,
            "primary_mother_count": primary_mother_count,
            "joined_member_count": joined_member_count,
            "invited": invited,
            "managed": reconciliation["managed"],
            "matched": reconciliation["matched"],
            "remote_only": reconciliation["remote_only"],
            "local_only": reconciliation["local_only"],
            "seat_limit": workspace.seat_limit,
            "occupied_seats": getattr(workspace, "occupied_seats", None),
            "last_official_sync_at": stamp.isoformat(),
            "sync_timestamp": stamp.isoformat(),
            "reconciliation": reconciliation["items"],
            "created_local_accounts": 0,
            "deleted_local_accounts": 0,
            "removed_memberships": removed_memberships,
            "absence_reconciled": can_reconcile_absence,
        }
        await operation_store.mark_step(db, operation, "commit_snapshot", state="success", result=result)
        await operation_store.finish(db, operation, result)
        await db.commit()
        return {"ok": True, "operation_id": operation.public_id, **result}

    async def _read_only_collection(self, db, workspace, owner, kind):
        from app.application.tokens import decrypt_secret

        token = decrypt_secret(owner.access_token_encrypted) if owner else None
        if not token:
            return {"success": False, "error_code": "credentials_missing", "error": "Owner access token is missing"}
        reader = self.workspaces.client.get_members if kind == "members" else self.workspaces.client.get_invites
        return await reader(token, workspace.official_workspace_id, db, identifier=owner.email)

    async def list_snapshots(self, db: AsyncSession, workspace_id: int) -> list[WorkspaceOfficialMemberSnapshot]:
        return list(
            (
                await db.execute(
                    select(WorkspaceOfficialMemberSnapshot)
                    .where(WorkspaceOfficialMemberSnapshot.workspace_id == workspace_id)
                    .order_by(WorkspaceOfficialMemberSnapshot.normalized_email.asc())
                )
            ).scalars()
        )


workspace_sync_service = WorkspaceSyncService()
