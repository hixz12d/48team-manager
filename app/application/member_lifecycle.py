"""Local reconciliation after a confirmed remote departure, scoped to one team."""

from sqlalchemy import delete, select

from app.core.time import utcnow
from app.domain.identity.ids import normalize_email
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot


async def record_confirmed_departure(db, workspace, email):
    target = normalize_email(email)
    account = await db.scalar(select(Account).where(Account.email == target))
    if account:
        membership = await db.scalar(select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace.id, WorkspaceMembership.account_id == account.id,
        ))
        if membership:
            membership.membership_state = "removed"
            membership.removed_at = membership.removed_at or utcnow()
            membership.updated_at = utcnow()
    await db.execute(delete(WorkspaceOfficialMemberSnapshot).where(
        WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id,
        WorkspaceOfficialMemberSnapshot.normalized_email == target,
    ))
    # A mutation confirms one person's absence, not a fresh full-team capacity read.
    workspace.last_official_sync_state = "stale"
    workspace.updated_at = utcnow()
    await db.flush()


async def has_other_active_context(db, account, workspace_id):
    if account.local_purpose == "mother":
        return True
    membership = await db.scalar(select(WorkspaceMembership.id).where(
        WorkspaceMembership.account_id == account.id,
        WorkspaceMembership.workspace_id != workspace_id,
        WorkspaceMembership.membership_state.in_(("joined", "invited")),
    ).limit(1))
    owned = await db.scalar(select(Workspace.id).where(
        Workspace.owner_account_id == account.id, Workspace.id != workspace_id,
    ).limit(1))
    return membership is not None or owned is not None
