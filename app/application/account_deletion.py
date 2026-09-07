"""Explicit local-only deletion of unassigned accounts; no provider calls."""

from sqlalchemy import delete, func, or_, select, update

from app.core.time import utcnow
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot
from app.persistence.models.oauth import OAuthSession
from app.persistence.models.operations import Operation
from app.persistence.models.quota import CredentialLease, QuotaProbeState, QuotaSnapshot
from app.persistence.models.resources import PhoneAttempt
from app.persistence.models.sub2api import Sub2ApiUsageSnapshot


class AccountDeletionError(ValueError):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code = code
        self.status = status


async def delete_unassigned_account(db, account_id: int, confirmation_email: str):
    # Obtain the SQLite writer lock before checking guards, so queue claims cannot
    # interleave with the check-and-delete transaction. The caller commits/rolls back.
    await db.execute(update(Account).where(Account.id == account_id).values(version=Account.version))
    account = await db.get(Account, account_id, populate_existing=True)
    if account is None:
        raise AccountDeletionError("not_found", "账号不存在或已删除。", 404)
    email = account.email.strip().lower()
    if confirmation_email.strip().lower() != email:
        raise AccountDeletionError("confirmation_mismatch", "确认邮箱不匹配，未删除。", 400)
    owned = await db.scalar(select(Workspace.id).where(Workspace.owner_account_id == account_id).limit(1))
    if account.local_purpose == "mother" or owned is not None:
        raise AccountDeletionError("account_is_owner", "母号或团队所有者不能从此入口删除。")
    linked = await db.scalar(select(WorkspaceMembership.id).where(
        WorkspaceMembership.account_id == account_id,
        WorkspaceMembership.membership_state != "removed",
    ).limit(1))
    remote = await db.scalar(select(WorkspaceOfficialMemberSnapshot.id).where(
        func.lower(WorkspaceOfficialMemberSnapshot.normalized_email) == email,
        WorkspaceOfficialMemberSnapshot.remote_state.in_(("joined", "invited")),
    ).limit(1))
    if linked is not None or remote is not None:
        raise AccountDeletionError("account_has_workspace", "账号仍有团队成员或邀请记录，请先在对应团队处理并同步。")
    now = utcnow()
    busy = await db.scalar(select(Operation.id).where(
        or_(Operation.account_id == account_id, func.lower(Operation.email) == email,
            (Operation.entity_type == "account") & (Operation.entity_id == account_id)),
        Operation.state.in_(("pending", "queued", "running", "waiting")),
    ).limit(1))
    oauth = await db.scalar(select(OAuthSession.id).where(
        or_(OAuthSession.account_id == account_id, func.lower(OAuthSession.email) == email),
        OAuthSession.status.in_(("waiting", "exchanging")), OAuthSession.expires_at > now,
    ).limit(1))
    probe = await db.scalar(select(QuotaProbeState.context_key).where(
        QuotaProbeState.account_id == account_id, QuotaProbeState.lease_expires_at > now,
    ).limit(1))
    credential = await db.scalar(select(CredentialLease.account_id).where(
        CredentialLease.account_id == account_id, CredentialLease.expires_at > now,
    ).limit(1))
    if any(value is not None for value in (busy, oauth, probe, credential)):
        raise AccountDeletionError("account_busy", "账号有执行中、排队中或待完成的授权任务，请结束任务后再删除。")

    for model, column in (
        (Sub2ApiUsageSnapshot, Sub2ApiUsageSnapshot.local_account_id),
        (ExternalBinding, ExternalBinding.local_account_id),
        (QuotaSnapshot, QuotaSnapshot.account_id),
        (QuotaProbeState, QuotaProbeState.account_id),
        (CredentialLease, CredentialLease.account_id),
        (OAuthSession, OAuthSession.account_id),
        (WorkspaceMembership, WorkspaceMembership.account_id),
    ):
        await db.execute(delete(model).where(column == account_id))
    await db.execute(update(Account).where(Account.source_child_account_id == account_id).values(source_child_account_id=None))
    await db.execute(update(Operation).where(Operation.account_id == account_id).values(account_id=None))
    await db.execute(update(Operation).where(Operation.entity_type == "account", Operation.entity_id == account_id).values(entity_id=None))
    await db.execute(update(PhoneAttempt).where(PhoneAttempt.account_id == account_id).values(account_id=None))
    # Preserve HME/phone occupancy and operation audit history; these are not free resources.
    await db.execute(delete(Account).where(Account.id == account_id))
    return {"ok": True, "deleted_account_id": account_id, "message": "本地账号档案已永久删除，远端账号及别名未改动。"}
