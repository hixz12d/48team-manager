"""Official Workspace member sync. Never invents local credential accounts."""

from __future__ import annotations

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.operations import operation_store
from app.application.workspaces import workspace_service
from app.core.time import utcnow
from app.domain.identity.ids import normalize_email
from app.persistence.models.identity import Account, WorkspaceMembership, WorkspaceOfficialMemberSnapshot


def _remote_email(item: dict[str, Any]) -> str:
    return normalize_email(str(item.get("email") or item.get("email_address") or ""))


def _remote_user_id(item: dict[str, Any]) -> str | None:
    for key in ("id", "user_id", "userId", "account_user_id"):
        value = item.get(key)
        if value:
            return str(value)
    user = item.get("user")
    if isinstance(user, dict):
        for key in ("id", "user_id"):
            value = user.get(key)
            if value:
                return str(value)
    return None


def _remote_role(item: dict[str, Any]) -> str:
    role = str(item.get("role") or item.get("official_role") or "").strip()
    return role or "unknown"


def _extract_seat_limit(*payloads: dict[str, Any]) -> int | None:
    for payload in payloads:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if not isinstance(data, dict):
            continue
        for key in ("seat_limit", "seats_limit", "max_seats", "seatLimit", "capacity"):
            value = data.get(key)
            if value is None:
                continue
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            if number > 0:
                return number
    return None


class WorkspaceSyncService:
    def __init__(self, workspaces=None):
        self.workspaces = workspaces or workspace_service

    async def sync_workspace(self, db: AsyncSession, workspace_id: int) -> dict[str, Any]:
        workspace = await self.workspaces.load_workspace(db, workspace_id)
        if workspace is None:
            return {"success": False, "error": "workspace not found", "error_code": "not_found"}

        owner = await self.workspaces.owner_account(db, workspace)
        operation = await operation_store.create(
            db,
            op_type="workspace_sync",
            workspace_id=workspace.id,
            account_id=workspace.owner_account_id,
            email=(owner.email if owner else "") or "",
            input_payload={"workspace_id": workspace.id},
        )
        await operation_store.note(db, operation, "fetch_members", "fetching official members")
        members = await self.workspaces.get_members(db, workspace)
        if not members.get("success"):
            result = {
                "success": False,
                "error": members.get("error") or "failed to fetch members",
                "error_code": members.get("error_code") or "members_failed",
                "status": "failed",
            }
            await operation_store.mark_step(
                db,
                operation,
                "fetch_members",
                state="failed",
                error_code=result["error_code"],
                error_message=result["error"],
            )
            await operation_store.finish(db, operation, result)
            await db.commit()
            return {"ok": False, "operation_id": operation.public_id, **result}

        await operation_store.mark_step(db, operation, "fetch_members", state="success", result={"total": members.get("total")})
        await operation_store.note(db, operation, "fetch_invites", "fetching official invites")
        invites = await self.workspaces.get_invites(db, workspace)
        if not invites.get("success"):
            result = {
                "success": False,
                "error": invites.get("error") or "failed to fetch invites",
                "error_code": invites.get("error_code") or "invites_failed",
                "status": "failed",
            }
            await operation_store.mark_step(
                db,
                operation,
                "fetch_invites",
                state="failed",
                error_code=result["error_code"],
                error_message=result["error"],
            )
            await operation_store.finish(db, operation, result)
            await db.commit()
            return {"ok": False, "operation_id": operation.public_id, **result}

        await operation_store.mark_step(db, operation, "fetch_invites", state="success", result={"total": invites.get("total")})
        stamp = utcnow()
        remote_rows: dict[str, dict[str, Any]] = {}
        for item in members.get("members") or []:
            if not isinstance(item, dict):
                continue
            email = _remote_email(item)
            if not email:
                continue
            remote_rows[email] = {
                "normalized_email": email,
                "official_user_id": _remote_user_id(item),
                "official_role": _remote_role(item),
                "remote_state": "joined",
            }
        for item in invites.get("items") or []:
            if not isinstance(item, dict):
                continue
            email = _remote_email(item)
            if not email:
                continue
            existing = remote_rows.get(email)
            if existing and existing.get("remote_state") == "joined":
                continue
            remote_rows[email] = {
                "normalized_email": email,
                "official_user_id": _remote_user_id(item),
                "official_role": _remote_role(item),
                "remote_state": "invited",
            }

        await db.execute(
            delete(WorkspaceOfficialMemberSnapshot).where(WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id)
        )
        for row in remote_rows.values():
            db.add(
                WorkspaceOfficialMemberSnapshot(
                    workspace_id=workspace.id,
                    normalized_email=row["normalized_email"],
                    official_user_id=row.get("official_user_id"),
                    official_role=row.get("official_role") or "unknown",
                    remote_state=row["remote_state"],
                    fetched_at=stamp,
                    created_at=stamp,
                    updated_at=stamp,
                )
            )

        seat_limit = _extract_seat_limit(members, invites)
        if seat_limit is not None:
            workspace.seat_limit = seat_limit
        workspace.last_official_sync_at = stamp
        workspace.updated_at = stamp
        workspace.version = int(workspace.version or 1) + 1

        local_accounts = {
            account.id: account
            for account in (await db.execute(select(Account))).scalars()
        }
        local_memberships = list(
            (
                await db.execute(select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == workspace.id))
            ).scalars()
        )
        local_by_email = {}
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

        reconciliation = []
        only_remote = 0
        only_local = 0
        matched = 0
        emails = sorted(set(remote_rows) | set(local_by_email))
        for email in emails:
            remote = remote_rows.get(email)
            local = local_by_email.get(email)
            if remote and local:
                diff = "matched"
                matched += 1
            elif remote and not local:
                diff = "remote_only"
                only_remote += 1
            else:
                diff = "local_only"
                only_local += 1
            reconciliation.append(
                {
                    "email": email,
                    "diff": diff,
                    "remote_state": (remote or {}).get("remote_state"),
                    "official_role": (remote or {}).get("official_role") or (local or {}).get("official_role"),
                    "local_account_id": (local or {}).get("account_id"),
                    "local_membership_state": (local or {}).get("membership_state"),
                }
            )

        result = {
            "success": True,
            "status": "success",
            "message": "official members synced",
            "workspace_id": workspace.id,
            "joined": sum(1 for row in remote_rows.values() if row["remote_state"] == "joined"),
            "invited": sum(1 for row in remote_rows.values() if row["remote_state"] == "invited"),
            "matched": matched,
            "remote_only": only_remote,
            "local_only": only_local,
            "seat_limit": workspace.seat_limit,
            "last_official_sync_at": stamp.isoformat(),
            "reconciliation": reconciliation,
            "created_local_accounts": 0,
            "deleted_local_accounts": 0,
        }
        await operation_store.mark_step(db, operation, "commit_snapshot", state="success", result=result)
        await operation_store.finish(db, operation, result)
        await db.commit()
        return {"ok": True, "operation_id": operation.public_id, **result}

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
