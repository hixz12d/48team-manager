"""Identity application service: persist, audit, bind, gate."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.time import utcnow
from app.domain.identity import (
    AUTH_STATE_UNKNOWN,
    BINDING_CONFLICT,
    BINDING_MISSING,
    BINDING_PENDING,
    BINDING_VERIFIED,
    LOCAL_PURPOSE_CHILD,
    LOCAL_PURPOSE_MOTHER,
    MEMBERSHIP_STATE_JOINED,
    MEMBERSHIP_STATE_UNKNOWN,
    OFFICIAL_PLAN_UNKNOWN,
    OFFICIAL_ROLE_OWNER,
    PROVIDER_SUB2API,
)
from app.domain.identity.audit import build_audit_report
from app.domain.identity.binding import (
    cross_check_binding,
    expected_workspace_id,
    match_unbound_local_account,
    remote_email_from,
    remote_id_from,
    remote_official_account_id_from,
    remote_snapshot,
    remote_workspace_id_from,
    serialize_binding,
)
from app.domain.identity.ids import normalize_email
from app.domain.identity.policy import (
    child_local_purpose,
    child_operational_state,
    normalize_local_purpose,
    membership_local_purpose_for,
    is_workspace_owner,
    normalize_operational_state,
    normalize_workspace_status,
)
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from app.persistence.repositories import identity as identity_repo


def _now():
    return utcnow()


def _remote_id(value: Any) -> str:
    if value in (None, "", 0, "0"):
        return ""
    return str(value).strip()


async def audit_identity(db: AsyncSession) -> dict[str, Any]:
    accounts = await identity_repo.list_accounts(db)
    memberships = await identity_repo.list_memberships(db)
    bindings = await identity_repo.list_bindings(db)
    workspaces = await identity_repo.list_workspaces(db)
    return build_audit_report(accounts, memberships, bindings, workspaces)


async def ensure_membership(
    db: AsyncSession,
    *,
    workspace_id: int,
    account_id: int,
    official_role: str,
    membership_state: str,
    local_purpose: str,
    joined_at=None,
    removed_at=None,
    source_mapping_id: int | None = None,
) -> bool:
    workspace = await db.get(Workspace, int(workspace_id))
    purpose = membership_local_purpose_for(workspace, account_id)
    if not purpose:
        purpose = normalize_local_purpose(local_purpose)
    result = await db.execute(
        select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.account_id == account_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        db.add(
            WorkspaceMembership(
                workspace_id=workspace_id,
                account_id=account_id,
                official_role=official_role,
                membership_state=membership_state,
                local_purpose=purpose,
                joined_at=joined_at,
                removed_at=removed_at,
                source_mapping_id=source_mapping_id,
            )
        )
        await db.flush()
        return True
    row.official_role = official_role or row.official_role
    row.local_purpose = purpose
    row.membership_state = membership_state
    if joined_at is not None:
        row.joined_at = joined_at
    row.removed_at = removed_at
    if source_mapping_id:
        row.source_mapping_id = source_mapping_id
    return False


async def ensure_binding(
    db: AsyncSession,
    *,
    account: Account,
    remote_account_id: Any,
    workspace_id: int | None = None,
) -> bool:
    remote_id = _remote_id(remote_account_id)
    if not remote_id:
        return False

    local_filter = [
        ExternalBinding.provider == PROVIDER_SUB2API,
        ExternalBinding.local_account_id == account.id,
    ]
    if workspace_id is None:
        local_filter.append(ExternalBinding.workspace_id.is_(None))
    else:
        local_filter.append(ExternalBinding.workspace_id == int(workspace_id))
    existing = (await db.execute(select(ExternalBinding).where(*local_filter))).scalar_one_or_none()
    if existing is None and workspace_id is not None:
        existing = (
            await db.execute(
                select(ExternalBinding).where(
                    ExternalBinding.provider == PROVIDER_SUB2API,
                    ExternalBinding.local_account_id == account.id,
                    ExternalBinding.workspace_id.is_(None),
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.workspace_id = int(workspace_id)
    if existing is not None:
        if existing.remote_account_id != remote_id:
            existing.binding_state = BINDING_CONFLICT
            existing.last_error = (
                f"local account already bound to remote {existing.remote_account_id}, "
                f"refusing {remote_id}"
            )
        elif workspace_id is not None:
            existing.workspace_id = int(workspace_id)
        return False

    taken = (
        await db.execute(
            select(ExternalBinding).where(
                ExternalBinding.provider == PROVIDER_SUB2API,
                ExternalBinding.remote_account_id == remote_id,
            )
        )
    ).scalar_one_or_none()
    if taken is not None and (taken.local_account_id != account.id or taken.workspace_id != workspace_id):
        taken.binding_state = BINDING_CONFLICT
        taken.last_error = (
            f"remote id {remote_id} already bound to local account {taken.local_account_id}; "
            f"refusing local account {account.id}"
        )
        return False

    db.add(
        ExternalBinding(
            provider=PROVIDER_SUB2API,
            local_account_id=account.id,
            remote_account_id=remote_id,
            workspace_id=int(workspace_id) if workspace_id is not None else None,
            binding_state=BINDING_PENDING,
        )
    )
    await db.flush()
    return True


async def mark_duplicate_remote_bindings(db: AsyncSession) -> int:
    rows = (await db.execute(select(ExternalBinding))).scalars().all()
    grouped: dict[tuple[str, str], list[ExternalBinding]] = defaultdict(list)
    for row in rows:
        grouped[(row.provider, row.remote_account_id)].append(row)
    conflicts = 0
    for items in grouped.values():
        if len(items) < 2:
            continue
        for item in items:
            if item.binding_state != BINDING_CONFLICT:
                item.binding_state = BINDING_CONFLICT
                item.last_error = "duplicate remote id bound to multiple local accounts"
                conflicts += 1
    return conflicts


def _write_binding(
    binding: ExternalBinding,
    state: str,
    *,
    error: str | None,
    remote: dict[str, Any] | None = None,
    observed: bool = True,
) -> None:
    if binding.binding_state == BINDING_CONFLICT and state != BINDING_CONFLICT:
        if observed:
            binding.last_observed_at = _now()
        if error:
            binding.last_error = error
        return
    binding.binding_state = state
    binding.last_error = error
    if observed:
        binding.last_observed_at = _now()
    if state == BINDING_VERIFIED and remote is not None:
        binding.verified_email = remote_email_from(remote) or None
        binding.verified_official_account_id = remote_official_account_id_from(remote) or None
        binding.verified_workspace_id = remote_workspace_id_from(remote)


async def verify_bindings(
    db: AsyncSession,
    remote_accounts: Sequence[dict[str, Any]],
) -> dict[str, Any]:
    """Cross-check against a provided Sub2API snapshot. No live HTTP."""
    remotes_by_id: dict[str, dict[str, Any]] = {}
    for item in remote_accounts or []:
        if not isinstance(item, dict):
            continue
        remote_id = remote_id_from(item)
        if not remote_id or remote_id in remotes_by_id:
            continue
        remotes_by_id[remote_id] = item

    accounts = await identity_repo.list_accounts(db)
    accounts_by_id = {row.id: row for row in accounts}
    accounts_by_email: dict[str, Account] = {}
    accounts_by_official: dict[str, list[Account]] = defaultdict(list)
    for account in accounts:
        accounts_by_email[normalize_email(account.email)] = account
        official = str(account.official_account_id or "").strip()
        if official:
            accounts_by_official[official].append(account)

    memberships = await identity_repo.list_memberships(db)
    memberships_by_account: dict[int, list[WorkspaceMembership]] = defaultdict(list)
    for row in memberships:
        memberships_by_account[row.account_id].append(row)
    workspaces_by_id = {row.id: row for row in await identity_repo.list_workspaces(db)}

    bindings = await identity_repo.list_bindings(db, PROVIDER_SUB2API)
    bindings_by_remote = {row.remote_account_id: row for row in bindings}
    bindings_by_local = {row.local_account_id: row for row in bindings}

    stats = {
        "observed": 0,
        "verified": 0,
        "pending": 0,
        "conflict": 0,
        "missing": 0,
        "orphaned": 0,
        "created": 0,
    }
    orphans: list[dict[str, Any]] = []

    for binding in list(bindings):
        remote = remotes_by_id.get(binding.remote_account_id)
        local = accounts_by_id.get(binding.local_account_id)
        if local is None:
            _write_binding(
                binding,
                BINDING_CONFLICT,
                error=f"local account {binding.local_account_id} missing",
                observed=False,
            )
            continue
        if remote is None:
            _write_binding(
                binding,
                BINDING_MISSING,
                error=f"remote id {binding.remote_account_id} missing from snapshot",
                observed=False,
            )
            continue
        stats["observed"] += 1
        expected = expected_workspace_id(
            local,
            memberships=memberships_by_account.get(local.id, []),
            workspaces_by_id=workspaces_by_id,
            workspace_id=getattr(binding, "workspace_id", None),
            allow_ambiguous=True,
        )
        state, error = cross_check_binding(
            local_email=local.email,
            local_official_account_id=local.official_account_id,
            expected_workspace=expected,
            remote=remote,
        )
        _write_binding(binding, state, error=error, remote=remote)

    for remote_id, remote in remotes_by_id.items():
        if remote_id in bindings_by_remote:
            continue
        local, match_error = match_unbound_local_account(
            remote,
            accounts_by_email=accounts_by_email,
            accounts_by_official=accounts_by_official,
        )
        snapshot = remote_snapshot(remote)
        if local is None:
            stats["orphaned"] += 1
            orphans.append({**snapshot, "reason": match_error or "no local account matched"})
            continue
        existing_local = bindings_by_local.get(local.id)
        if existing_local is not None and existing_local.remote_account_id != remote_id:
            _write_binding(
                existing_local,
                BINDING_CONFLICT,
                error=(
                    f"local account {local.id} already bound to remote {existing_local.remote_account_id}, "
                    f"refusing {remote_id}"
                ),
                remote=remote,
            )
            continue
        binding = ExternalBinding(
            provider=PROVIDER_SUB2API,
            local_account_id=local.id,
            remote_account_id=remote_id,
            binding_state=BINDING_PENDING,
        )
        db.add(binding)
        await db.flush()
        bindings.append(binding)
        bindings_by_remote[remote_id] = binding
        bindings_by_local[local.id] = binding
        stats["created"] += 1
        stats["observed"] += 1
        expected = expected_workspace_id(
            local,
            memberships=memberships_by_account.get(local.id, []),
            workspaces_by_id=workspaces_by_id,
            workspace_id=getattr(binding, "workspace_id", None),
            allow_ambiguous=True,
        )
        state, error = cross_check_binding(
            local_email=local.email,
            local_official_account_id=local.official_account_id,
            expected_workspace=expected,
            remote=remote,
        )
        _write_binding(binding, state, error=error, remote=remote)

    await db.flush()
    await mark_duplicate_remote_bindings(db)
    await db.commit()

    refreshed = await identity_repo.list_bindings(db, PROVIDER_SUB2API)
    for row in refreshed:
        state = row.binding_state or BINDING_PENDING
        if state in stats:
            stats[state] += 1

    return {
        "stats": stats,
        "orphans": orphans,
        "bindings": [serialize_binding(row) for row in refreshed],
    }


async def automation_gate(
    db: AsyncSession,
    *,
    remote_account_id: Any = None,
    email: str = "",
    workspace_id: int | None = None,
) -> dict[str, Any]:
    """Stop automation on conflict / owner. Never guess from Gmail or names."""
    target_email = normalize_email(email)
    remote_id = _remote_id(remote_account_id)

    binding = None
    if remote_id:
        binding = (
            await db.execute(
                select(ExternalBinding).where(
                    ExternalBinding.provider == PROVIDER_SUB2API,
                    ExternalBinding.remote_account_id == remote_id,
                )
            )
        ).scalar_one_or_none()

    account = None
    if binding is not None:
        account = await db.get(Account, binding.local_account_id)
    if account is None and target_email:
        account = (
            await db.execute(select(Account).where(Account.email == target_email))
        ).scalar_one_or_none()

    def payload(
        *,
        allow: bool,
        decision: str,
        error_code: str,
        reason: str,
        source: str,
        account_id: int | None = None,
        local_purpose: str = "",
        binding_state: str = "",
    ) -> dict[str, Any]:
        return {
            "allow": allow,
            "decision": decision,
            "error_code": error_code,
            "reason": reason,
            "account_id": account_id,
            "local_purpose": local_purpose,
            "binding_state": binding_state,
            "source": source,
        }

    if account is not None:
        if binding is None:
            binding = (
                await db.execute(
                    select(ExternalBinding).where(
                        ExternalBinding.provider == PROVIDER_SUB2API,
                        ExternalBinding.local_account_id == account.id,
                    )
                )
            ).scalar_one_or_none()
        binding_state = str(binding.binding_state or "") if binding is not None else ""
        memberships = (
            await db.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.account_id == account.id)
            )
        ).scalars().all()
        owned_workspaces = list(
            (await db.execute(select(Workspace).where(Workspace.owner_account_id == account.id))).scalars()
        )
        if workspace_id is not None:
            current_workspace = await db.get(Workspace, int(workspace_id))
            is_primary_mother = is_workspace_owner(current_workspace, account.id)
        else:
            is_primary_mother = account.local_purpose == LOCAL_PURPOSE_MOTHER or bool(owned_workspaces)
        if binding_state == BINDING_CONFLICT:
            return payload(
                allow=False,
                decision="conflict",
                error_code="identity_conflict",
                reason=str(binding.last_error or "Sub2API 绑定 conflict，停止自动重授权"),
                source="identity",
                account_id=account.id,
                local_purpose=account.local_purpose or "",
                binding_state=binding_state,
            )
        if is_primary_mother:
            return payload(
                allow=False,
                decision="owner",
                error_code="owner_manual",
                reason="当前工作区的主控母号，不自动重授权",
                source="identity",
                account_id=account.id,
                local_purpose=account.local_purpose or "",
                binding_state=binding_state,
            )
        if str(account.operational_state or "") in {
            "standby",
            "disabled",
            "archived",
            "free",
            "unused",
        }:
            return payload(
                allow=False,
                decision="skip",
                error_code="identity_inactive",
                reason=f"本地 operational_state={account.operational_state}，不自动重授权",
                source="identity",
                account_id=account.id,
                local_purpose=account.local_purpose or "",
                binding_state=binding_state,
            )
        return payload(
            allow=True,
            decision="allow",
            error_code="",
            reason="identity 允许自动重授权",
            source="identity",
            account_id=account.id,
            local_purpose=account.local_purpose or LOCAL_PURPOSE_CHILD,
            binding_state=binding_state,
        )

    return payload(
        allow=False,
        decision="unbound",
        error_code="identity_unbound",
        reason="对不上本地账号，停止自动重授权",
        source="identity",
    )


async def upsert_mother_account(
    db: AsyncSession,
    *,
    email: str,
    source_team_id: int | None = None,
    operational_state: str = "active",
    proxy: str | None = None,
    access_token_encrypted: str | None = None,
    refresh_token_encrypted: str | None = None,
    session_token_encrypted: str | None = None,
    id_token_encrypted: str | None = None,
    client_id: str | None = None,
) -> tuple[Account, bool]:
    email_n = normalize_email(email)
    account = (await db.execute(select(Account).where(Account.email == email_n))).scalar_one_or_none()
    if account is None:
        account = Account(
            email=email_n,
            official_plan=OFFICIAL_PLAN_UNKNOWN,
            official_user_id=None,
            official_account_id=None,
            auth_state=AUTH_STATE_UNKNOWN,
            operational_state=normalize_operational_state(operational_state),
            local_purpose=LOCAL_PURPOSE_MOTHER,
            proxy=proxy,
            access_token_encrypted=access_token_encrypted,
            refresh_token_encrypted=refresh_token_encrypted,
            session_token_encrypted=session_token_encrypted,
            id_token_encrypted=id_token_encrypted,
            client_id=client_id,
            source_team_id=source_team_id,
        )
        db.add(account)
        await db.flush()
        return account, True
    account.local_purpose = LOCAL_PURPOSE_MOTHER
    if source_team_id is not None:
        account.source_team_id = source_team_id
    if proxy and not account.proxy:
        account.proxy = proxy
    if access_token_encrypted and not account.access_token_encrypted:
        account.access_token_encrypted = access_token_encrypted
        account.refresh_token_encrypted = refresh_token_encrypted
        account.session_token_encrypted = session_token_encrypted
        account.id_token_encrypted = id_token_encrypted
        account.client_id = client_id
    return account, False


async def upsert_child_account(
    db: AsyncSession,
    *,
    email: str,
    status: str | None = None,
    source_child_account_id: int | None = None,
    proxy: str | None = None,
    access_token_encrypted: str | None = None,
    refresh_token_encrypted: str | None = None,
    session_token_encrypted: str | None = None,
    id_token_encrypted: str | None = None,
    client_id: str | None = None,
    next_eligible_at=None,
) -> tuple[Account, bool]:
    email_n = normalize_email(email)
    purpose = child_local_purpose(status)
    state = child_operational_state(status)
    account = (await db.execute(select(Account).where(Account.email == email_n))).scalar_one_or_none()
    if account is None:
        account = Account(
            email=email_n,
            official_plan=OFFICIAL_PLAN_UNKNOWN,
            official_user_id=None,
            official_account_id=None,
            auth_state=AUTH_STATE_UNKNOWN,
            operational_state=state,
            local_purpose=purpose,
            proxy=proxy,
            access_token_encrypted=access_token_encrypted,
            refresh_token_encrypted=refresh_token_encrypted,
            session_token_encrypted=session_token_encrypted,
            id_token_encrypted=id_token_encrypted,
            client_id=client_id,
            next_eligible_at=next_eligible_at,
            source_child_account_id=source_child_account_id,
        )
        db.add(account)
        await db.flush()
        return account, True
    if account.local_purpose != LOCAL_PURPOSE_MOTHER:
        account.local_purpose = purpose
        account.operational_state = state
    if source_child_account_id is not None:
        account.source_child_account_id = source_child_account_id
    if next_eligible_at and not account.next_eligible_at:
        account.next_eligible_at = next_eligible_at
    if proxy and not account.proxy:
        account.proxy = proxy
    if access_token_encrypted and not account.access_token_encrypted:
        account.access_token_encrypted = access_token_encrypted
        account.refresh_token_encrypted = refresh_token_encrypted
        account.session_token_encrypted = session_token_encrypted
        account.id_token_encrypted = id_token_encrypted
        account.client_id = client_id
    return account, False


async def upsert_workspace(
    db: AsyncSession,
    *,
    source_team_id: int,
    official_workspace_id: str | None,
    name: str | None,
    subscription_plan: str | None,
    owner_account_id: int,
    status: str | None,
    seat_limit: int | None,
    last_official_sync_at=None,
) -> tuple[Workspace, bool]:
    workspace = (
        await db.execute(select(Workspace).where(Workspace.source_team_id == source_team_id))
    ).scalar_one_or_none()
    if workspace is None:
        workspace = Workspace(
            official_workspace_id=official_workspace_id,
            name=name,
            subscription_plan=subscription_plan,
            owner_account_id=owner_account_id,
            status=normalize_workspace_status(status),
            seat_limit=seat_limit,
            last_official_sync_at=last_official_sync_at,
            source_team_id=source_team_id,
        )
        db.add(workspace)
        await db.flush()
        return workspace, True
    workspace.owner_account_id = owner_account_id
    workspace.official_workspace_id = official_workspace_id
    if name:
        workspace.name = name
    workspace.subscription_plan = subscription_plan or workspace.subscription_plan
    workspace.status = normalize_workspace_status(status or workspace.status)
    workspace.seat_limit = seat_limit
    workspace.last_official_sync_at = last_official_sync_at
    return workspace, False
