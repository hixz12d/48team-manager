"""Phase 1 Identity：新表回填与只读审计。

本地数据库认人。Gmail / 名字 / family 不是身份真相。
旧 Team / ChildAccount 读写路径不走这里。
"""
from __future__ import annotations

import argparse
import json
import logging
from collections import defaultdict
from typing import Any, Dict, List, Optional, Sequence, Tuple

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    Account,
    ChildAccount,
    ExternalBinding,
    Team,
    TeamEmailMapping,
    Workspace,
    WorkspaceMembership,
)
from app.services.child_accounts import (
    CHILD_STATUS_ACTIVE,
    CHILD_STATUS_DELETED,
    CHILD_STATUS_DISABLED,
    CHILD_STATUS_FREE,
    CHILD_STATUS_INVITED,
    CHILD_STATUS_STANDBY,
    CHILD_STATUS_UNUSED,
    is_workspace_account_id,
    normalize_email,
)
from app.utils.time_utils import get_now

logger = logging.getLogger(__name__)

PROVIDER_SUB2API = "sub2api"

AUDIT_VERIFIED = "verified"
AUDIT_CONFLICT = "conflict"
AUDIT_UNBOUND = "unbound"
AUDIT_SUSPICIOUS = "suspicious"

LOCAL_PURPOSE_MOTHER = "mother"
LOCAL_PURPOSE_CHILD = "child"
LOCAL_PURPOSE_STANDBY = "standby"
LOCAL_PURPOSE_FREE = "free"
LOCAL_PURPOSE_DISABLED = "disabled"

OFFICIAL_PLAN_UNKNOWN = "unknown"
AUTH_STATE_UNKNOWN = "unknown"

MEMBERSHIP_STATE_INVITED = "invited"
MEMBERSHIP_STATE_JOINED = "joined"
MEMBERSHIP_STATE_REMOVED = "removed"
MEMBERSHIP_STATE_UNKNOWN = "unknown"

OFFICIAL_ROLE_OWNER = "owner"
OFFICIAL_ROLE_UNKNOWN = "unknown"

BINDING_PENDING = "pending"
BINDING_CONFLICT = "conflict"

GMAIL_DOMAINS = ("gmail.com", "googlemail.com")


def looks_like_gmail(email: str) -> bool:
    normalized = normalize_email(email)
    if "@" not in normalized:
        return False
    return normalized.rsplit("@", 1)[1] in GMAIL_DOMAINS


def looks_like_user_id(value: Optional[str]) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith("user-")


def child_local_purpose(status: Optional[str]) -> str:
    if status == CHILD_STATUS_FREE:
        return LOCAL_PURPOSE_FREE
    if status == CHILD_STATUS_STANDBY:
        return LOCAL_PURPOSE_STANDBY
    if status in (CHILD_STATUS_DISABLED, CHILD_STATUS_DELETED):
        return LOCAL_PURPOSE_DISABLED
    return LOCAL_PURPOSE_CHILD


def child_operational_state(status: Optional[str]) -> str:
    mapping = {
        CHILD_STATUS_UNUSED: "unused",
        CHILD_STATUS_INVITED: "available",
        CHILD_STATUS_ACTIVE: "active",
        CHILD_STATUS_STANDBY: "standby",
        CHILD_STATUS_DISABLED: "disabled",
        CHILD_STATUS_DELETED: "archived",
        CHILD_STATUS_FREE: "free",
    }
    return mapping.get(status or "", "available")


def mapping_membership_state(status: Optional[str]) -> str:
    if status in (MEMBERSHIP_STATE_INVITED, MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_REMOVED):
        return status
    return MEMBERSHIP_STATE_UNKNOWN


def workspace_official_id(team: Team) -> Optional[str]:
    value = str(team.account_id or "").strip()
    if not value:
        return None
    if looks_like_user_id(value) or not is_workspace_account_id(value):
        return None
    return value


def _now():
    return get_now()


class IdentityService:
    async def backfill(self, db_session: AsyncSession) -> Dict[str, int]:
        """按 docs/refactor-current-dataflow.md §16 把旧表投影到新 identity 表。幂等。"""
        stats = {
            "mother_accounts": 0,
            "child_accounts": 0,
            "workspaces": 0,
            "owner_memberships": 0,
            "mapping_memberships": 0,
            "bindings": 0,
            "binding_conflicts": 0,
            "skipped_user_ids": 0,
        }

        teams = (await db_session.execute(select(Team).order_by(Team.id))).scalars().all()
        children = (await db_session.execute(select(ChildAccount).order_by(ChildAccount.id))).scalars().all()
        mappings = (await db_session.execute(select(TeamEmailMapping).order_by(TeamEmailMapping.id))).scalars().all()

        accounts_by_email: Dict[str, Account] = {}
        existing_accounts = (await db_session.execute(select(Account))).scalars().all()
        for account in existing_accounts:
            accounts_by_email[normalize_email(account.email)] = account

        workspaces_by_team: Dict[int, Workspace] = {}
        existing_workspaces = (await db_session.execute(select(Workspace))).scalars().all()
        for workspace in existing_workspaces:
            if workspace.source_team_id:
                workspaces_by_team[int(workspace.source_team_id)] = workspace

        for team in teams:
            email = normalize_email(team.email)
            if not email:
                continue
            account = accounts_by_email.get(email)
            if account is None:
                account = Account(
                    email=email,
                    official_plan=OFFICIAL_PLAN_UNKNOWN,
                    official_user_id=None,
                    official_account_id=None,
                    auth_state=AUTH_STATE_UNKNOWN,
                    operational_state="active" if (team.status or "active") == "active" else (team.status or "active"),
                    local_purpose=LOCAL_PURPOSE_MOTHER,
                    proxy=team.proxy,
                    access_token_encrypted=team.access_token_encrypted,
                    refresh_token_encrypted=team.refresh_token_encrypted,
                    session_token_encrypted=team.session_token_encrypted,
                    id_token_encrypted=team.id_token_encrypted,
                    client_id=team.client_id,
                    source_team_id=team.id,
                    created_at=team.created_at or _now(),
                    updated_at=_now(),
                )
                db_session.add(account)
                await db_session.flush()
                accounts_by_email[email] = account
                stats["mother_accounts"] += 1
            else:
                account.local_purpose = LOCAL_PURPOSE_MOTHER
                account.source_team_id = team.id
                if team.proxy and not account.proxy:
                    account.proxy = team.proxy
                if team.access_token_encrypted and not account.access_token_encrypted:
                    account.access_token_encrypted = team.access_token_encrypted
                    account.refresh_token_encrypted = team.refresh_token_encrypted
                    account.session_token_encrypted = team.session_token_encrypted
                    account.id_token_encrypted = team.id_token_encrypted
                    account.client_id = team.client_id

            official_id = workspace_official_id(team)
            if team.account_id and official_id is None:
                stats["skipped_user_ids"] += 1
                logger.warning(
                    "Team %s account_id=%s 不是 Workspace UUID，不写入 official_workspace_id",
                    team.id,
                    team.account_id,
                )

            workspace = workspaces_by_team.get(team.id)
            if workspace is None:
                workspace = Workspace(
                    official_workspace_id=official_id,
                    name=team.team_name,
                    subscription_plan=team.subscription_plan or team.plan_type,
                    owner_account_id=account.id,
                    status=team.status or "active",
                    seat_limit=team.max_members,
                    last_official_sync_at=team.last_sync,
                    source_team_id=team.id,
                    created_at=team.created_at or _now(),
                    updated_at=_now(),
                )
                db_session.add(workspace)
                await db_session.flush()
                workspaces_by_team[team.id] = workspace
                stats["workspaces"] += 1
            else:
                workspace.owner_account_id = account.id
                workspace.official_workspace_id = official_id
                if team.team_name:
                    workspace.name = team.team_name
                workspace.subscription_plan = team.subscription_plan or team.plan_type or workspace.subscription_plan
                workspace.status = team.status or workspace.status
                workspace.seat_limit = team.max_members
                workspace.last_official_sync_at = team.last_sync

            if await self._ensure_membership(
                db_session,
                workspace_id=workspace.id,
                account_id=account.id,
                official_role=OFFICIAL_ROLE_OWNER,
                membership_state=MEMBERSHIP_STATE_JOINED,
                local_purpose=LOCAL_PURPOSE_MOTHER,
                joined_at=team.created_at,
            ):
                stats["owner_memberships"] += 1

            if await self._ensure_binding(
                db_session,
                account=account,
                remote_account_id=team.sub2api_account_id,
            ):
                stats["bindings"] += 1

        for child in children:
            email = normalize_email(child.email)
            if not email:
                continue
            account = accounts_by_email.get(email)
            purpose = child_local_purpose(child.status)
            state = child_operational_state(child.status)
            if account is None:
                account = Account(
                    email=email,
                    official_plan=OFFICIAL_PLAN_UNKNOWN,
                    official_user_id=None,
                    official_account_id=None,
                    auth_state=AUTH_STATE_UNKNOWN,
                    operational_state=state,
                    local_purpose=purpose,
                    proxy=child.proxy,
                    access_token_encrypted=child.access_token_encrypted,
                    refresh_token_encrypted=child.refresh_token_encrypted,
                    session_token_encrypted=child.session_token_encrypted,
                    id_token_encrypted=child.id_token_encrypted,
                    client_id=child.client_id,
                    next_eligible_at=child.next_eligible_at,
                    source_child_account_id=child.id,
                    created_at=child.created_at or _now(),
                    updated_at=_now(),
                )
                db_session.add(account)
                await db_session.flush()
                accounts_by_email[email] = account
                stats["child_accounts"] += 1
            else:
                if account.local_purpose != LOCAL_PURPOSE_MOTHER:
                    account.local_purpose = purpose
                    account.operational_state = state
                account.source_child_account_id = child.id
                if child.next_eligible_at and not account.next_eligible_at:
                    account.next_eligible_at = child.next_eligible_at
                if child.proxy and not account.proxy:
                    account.proxy = child.proxy
                if child.access_token_encrypted and not account.access_token_encrypted:
                    account.access_token_encrypted = child.access_token_encrypted
                    account.refresh_token_encrypted = child.refresh_token_encrypted
                    account.session_token_encrypted = child.session_token_encrypted
                    account.id_token_encrypted = child.id_token_encrypted
                    account.client_id = child.client_id

            if await self._ensure_binding(
                db_session,
                account=account,
                remote_account_id=child.sub2api_account_id,
            ):
                stats["bindings"] += 1

        for mapping in mappings:
            email = normalize_email(mapping.email)
            if not email:
                continue
            workspace = workspaces_by_team.get(mapping.team_id)
            if workspace is None:
                continue
            account = accounts_by_email.get(email)
            if account is None:
                account = Account(
                    email=email,
                    official_plan=OFFICIAL_PLAN_UNKNOWN,
                    auth_state=AUTH_STATE_UNKNOWN,
                    operational_state="available",
                    local_purpose=LOCAL_PURPOSE_CHILD,
                    created_at=mapping.created_at or _now(),
                    updated_at=_now(),
                )
                db_session.add(account)
                await db_session.flush()
                accounts_by_email[email] = account
                stats["child_accounts"] += 1

            if account.local_purpose == LOCAL_PURPOSE_MOTHER:
                # owner 行只从 Workspace.owner 来，不把 mapping 猜成 owner。
                continue

            if await self._ensure_membership(
                db_session,
                workspace_id=workspace.id,
                account_id=account.id,
                official_role=OFFICIAL_ROLE_UNKNOWN,
                membership_state=mapping_membership_state(mapping.status),
                local_purpose=LOCAL_PURPOSE_CHILD,
                joined_at=mapping.joined_at,
                removed_at=mapping.kicked_at,
                source_mapping_id=mapping.id,
            ):
                stats["mapping_memberships"] += 1

        await db_session.flush()
        conflicted = await self._mark_duplicate_remote_bindings(db_session)
        stats["binding_conflicts"] = conflicted
        await db_session.commit()
        return stats

    async def _ensure_membership(
        self,
        db_session: AsyncSession,
        *,
        workspace_id: int,
        account_id: int,
        official_role: str,
        membership_state: str,
        local_purpose: str,
        joined_at=None,
        removed_at=None,
        source_mapping_id: Optional[int] = None,
    ) -> bool:
        result = await db_session.execute(
            select(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.account_id == account_id,
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            db_session.add(
                WorkspaceMembership(
                    workspace_id=workspace_id,
                    account_id=account_id,
                    official_role=official_role,
                    membership_state=membership_state,
                    local_purpose=local_purpose,
                    joined_at=joined_at,
                    removed_at=removed_at,
                    source_mapping_id=source_mapping_id,
                    created_at=_now(),
                    updated_at=_now(),
                )
            )
            await db_session.flush()
            return True
        if official_role == OFFICIAL_ROLE_OWNER:
            row.official_role = OFFICIAL_ROLE_OWNER
            row.local_purpose = LOCAL_PURPOSE_MOTHER
            if row.membership_state in (None, MEMBERSHIP_STATE_UNKNOWN):
                row.membership_state = membership_state
        else:
            row.membership_state = membership_state
            row.joined_at = joined_at
            row.removed_at = removed_at
            if source_mapping_id:
                row.source_mapping_id = source_mapping_id
        row.updated_at = _now()
        return False

    async def _ensure_binding(
        self,
        db_session: AsyncSession,
        *,
        account: Account,
        remote_account_id: Any,
    ) -> bool:
        remote_id = str(remote_account_id).strip() if remote_account_id not in (None, "", 0, "0") else ""
        if not remote_id:
            return False

        existing = (
            await db_session.execute(
                select(ExternalBinding).where(
                    ExternalBinding.provider == PROVIDER_SUB2API,
                    ExternalBinding.local_account_id == account.id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            if existing.remote_account_id != remote_id:
                existing.binding_state = BINDING_CONFLICT
                existing.last_error = (
                    f"local account already bound to remote {existing.remote_account_id}, "
                    f"refusing {remote_id}"
                )
                existing.updated_at = _now()
            return False

        taken = (
            await db_session.execute(
                select(ExternalBinding).where(
                    ExternalBinding.provider == PROVIDER_SUB2API,
                    ExternalBinding.remote_account_id == remote_id,
                )
            )
        ).scalar_one_or_none()
        if taken is not None and taken.local_account_id != account.id:
            taken.binding_state = BINDING_CONFLICT
            taken.last_error = (
                f"remote id {remote_id} already bound to local account {taken.local_account_id}; "
                f"refusing local account {account.id}"
            )
            taken.updated_at = _now()
            logger.warning(taken.last_error)
            return False

        db_session.add(
            ExternalBinding(
                provider=PROVIDER_SUB2API,
                local_account_id=account.id,
                remote_account_id=remote_id,
                binding_state=BINDING_PENDING,
                created_at=_now(),
                updated_at=_now(),
            )
        )
        await db_session.flush()
        return True

    async def _mark_duplicate_remote_bindings(self, db_session: AsyncSession) -> int:
        rows = (await db_session.execute(select(ExternalBinding))).scalars().all()
        grouped: Dict[Tuple[str, str], List[ExternalBinding]] = defaultdict(list)
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
                    item.updated_at = _now()
                    conflicts += 1
        return conflicts

    async def audit(self, db_session: AsyncSession) -> Dict[str, Any]:
        accounts = (await db_session.execute(select(Account).order_by(Account.id))).scalars().all()
        memberships = (await db_session.execute(select(WorkspaceMembership))).scalars().all()
        bindings = (await db_session.execute(select(ExternalBinding))).scalars().all()
        workspaces = (await db_session.execute(select(Workspace))).scalars().all()

        memberships_by_account: Dict[int, List[WorkspaceMembership]] = defaultdict(list)
        for row in memberships:
            memberships_by_account[row.account_id].append(row)
        bindings_by_account: Dict[int, List[ExternalBinding]] = defaultdict(list)
        remote_owners: Dict[Tuple[str, str], List[ExternalBinding]] = defaultdict(list)
        for row in bindings:
            bindings_by_account[row.local_account_id].append(row)
            remote_owners[(row.provider, row.remote_account_id)].append(row)
        workspaces_by_id = {row.id: row for row in workspaces}

        findings: List[Dict[str, Any]] = []
        for account in accounts:
            findings.append(
                self._audit_account(
                    account,
                    memberships=memberships_by_account.get(account.id, []),
                    bindings=bindings_by_account.get(account.id, []),
                    remote_owners=remote_owners,
                    workspaces_by_id=workspaces_by_id,
                )
            )

        counts = {
            AUDIT_VERIFIED: 0,
            AUDIT_CONFLICT: 0,
            AUDIT_UNBOUND: 0,
            AUDIT_SUSPICIOUS: 0,
        }
        for item in findings:
            counts[item["result"]] = counts.get(item["result"], 0) + 1
        return {"counts": counts, "findings": findings}

    def _audit_account(
        self,
        account: Account,
        *,
        memberships: Sequence[WorkspaceMembership],
        bindings: Sequence[ExternalBinding],
        remote_owners: Dict[Tuple[str, str], List[ExternalBinding]],
        workspaces_by_id: Dict[int, Workspace],
    ) -> Dict[str, Any]:
        reasons: List[str] = []
        result = AUDIT_VERIFIED
        owner_memberships = [
            row for row in memberships
            if row.official_role == OFFICIAL_ROLE_OWNER
            and row.membership_state in (MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_UNKNOWN)
        ]
        active_memberships = [
            row for row in memberships
            if row.membership_state in (MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_INVITED)
        ]

        if looks_like_gmail(account.email) and not owner_memberships:
            result = AUDIT_CONFLICT
            reasons.append("Gmail 无 workspace owner membership")

        if account.local_purpose == LOCAL_PURPOSE_MOTHER and not owner_memberships:
            result = AUDIT_CONFLICT
            reasons.append("本地用途是母号，但没有 owner membership")

        if account.official_user_id and looks_like_user_id(account.official_user_id):
            for workspace in workspaces_by_id.values():
                if workspace.official_workspace_id and workspace.official_workspace_id == account.official_user_id:
                    result = AUDIT_CONFLICT
                    reasons.append("Workspace ID 与 user-xxx 混用")

        for workspace in workspaces_by_id.values():
            if workspace.owner_account_id == account.id and looks_like_user_id(workspace.official_workspace_id):
                result = AUDIT_CONFLICT
                reasons.append("official_workspace_id 写成了 user-xxx")
            if workspace.owner_account_id == account.id and workspace.official_workspace_id and not is_workspace_account_id(workspace.official_workspace_id):
                result = AUDIT_CONFLICT
                reasons.append("official_workspace_id 不是 Workspace UUID")

        for binding in bindings:
            siblings = remote_owners.get((binding.provider, binding.remote_account_id), [])
            if len(siblings) > 1 or binding.binding_state == BINDING_CONFLICT:
                result = AUDIT_CONFLICT
                reasons.append("一个 remote id 绑了多个本地账号，或绑定已标 conflict")

        if result != AUDIT_CONFLICT:
            if account.local_purpose == LOCAL_PURPOSE_MOTHER:
                workspace_ok = any(
                    workspaces_by_id[row.workspace_id].official_workspace_id
                    for row in owner_memberships
                    if row.workspace_id in workspaces_by_id
                    and is_workspace_account_id(workspaces_by_id[row.workspace_id].official_workspace_id)
                )
                if not workspace_ok:
                    result = AUDIT_SUSPICIOUS
                    reasons.append("母号缺少已验证的 Workspace UUID")
            elif not active_memberships and account.local_purpose == LOCAL_PURPOSE_CHILD:
                result = AUDIT_UNBOUND
                reasons.append("子号没有 invited/joined membership")
            elif account.local_purpose in (LOCAL_PURPOSE_STANDBY, LOCAL_PURPOSE_FREE, LOCAL_PURPOSE_DISABLED) and not bindings:
                result = AUDIT_UNBOUND
                reasons.append("没有 Sub2API binding")
            elif not bindings:
                result = AUDIT_UNBOUND
                reasons.append("没有 Sub2API binding")
            if result == AUDIT_VERIFIED and any(binding.binding_state == BINDING_PENDING for binding in bindings):
                reasons.append("Sub2API binding 仍是 pending，尚未 email / official id 交叉验证")

        if not reasons:
            if result == AUDIT_VERIFIED:
                reasons.append("本地用途、membership 与 Workspace UUID 一致")
            else:
                reasons.append(result)

        return {
            "account_id": account.id,
            "email": account.email,
            "local_purpose": account.local_purpose,
            "official_plan": account.official_plan,
            "official_user_id": account.official_user_id,
            "official_account_id": account.official_account_id,
            "operational_state": account.operational_state,
            "gmail": looks_like_gmail(account.email),
            "owner_memberships": len(owner_memberships),
            "memberships": [
                {
                    "workspace_id": row.workspace_id,
                    "official_role": row.official_role,
                    "membership_state": row.membership_state,
                    "local_purpose": row.local_purpose,
                }
                for row in memberships
            ],
            "bindings": [
                {
                    "provider": row.provider,
                    "remote_account_id": row.remote_account_id,
                    "binding_state": row.binding_state,
                    "last_error": row.last_error,
                }
                for row in bindings
            ],
            "result": result,
            "reasons": reasons,
            "automation": "blocked" if result == AUDIT_CONFLICT else "not_started",
        }

    def format_audit_text(self, report: Dict[str, Any]) -> str:
        lines = ["Identity Audit", "==============", ""]
        counts = report.get("counts") or {}
        lines.append(
            "Counts: Verified={verified} Conflict={conflict} Unbound={unbound} Suspicious={suspicious}".format(
                verified=counts.get(AUDIT_VERIFIED, 0),
                conflict=counts.get(AUDIT_CONFLICT, 0),
                unbound=counts.get(AUDIT_UNBOUND, 0),
                suspicious=counts.get(AUDIT_SUSPICIOUS, 0),
            )
        )
        lines.append("")
        for item in report.get("findings") or []:
            lines.append(f"Local account #{item.get('account_id')}")
            lines.append(f"  email: {item.get('email')}")
            lines.append(f"  official plan: {item.get('official_plan')}")
            lines.append(f"  local purpose: {item.get('local_purpose')}")
            memberships = item.get("memberships") or []
            if memberships:
                lines.append("  Workspace membership:")
                for row in memberships:
                    lines.append(
                        f"    workspace={row.get('workspace_id')} role={row.get('official_role')} "
                        f"state={row.get('membership_state')} purpose={row.get('local_purpose')}"
                    )
            else:
                lines.append("  Workspace membership: none")
            bindings = item.get("bindings") or []
            if bindings:
                lines.append("  Sub2API:")
                for row in bindings:
                    lines.append(
                        f"    remote id: {row.get('remote_account_id')} state={row.get('binding_state')}"
                    )
            else:
                lines.append("  Sub2API: none")
            lines.append(f"  RESULT: {str(item.get('result') or '').upper()}")
            lines.append("  Reason:")
            for reason in item.get("reasons") or []:
                lines.append(f"    {reason}")
            lines.append(f"  Automation: {str(item.get('automation') or '').upper()}")
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"


identity_service = IdentityService()


async def _run_cli(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Phase 1 Identity backfill / audit")
    parser.add_argument("command", choices=("backfill", "audit"), help="backfill 写入新表；audit 只读")
    parser.add_argument("--json", action="store_true", help="audit 输出 JSON")
    args = parser.parse_args(argv)

    from app.database import AsyncSessionLocal

    async with AsyncSessionLocal() as session:
        if args.command == "backfill":
            stats = await identity_service.backfill(session)
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 0
        report = await identity_service.audit(session)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        else:
            print(identity_service.format_audit_text(report), end="")
        return 0


def main(argv: Optional[Sequence[str]] = None) -> None:
    import asyncio

    raise SystemExit(asyncio.run(_run_cli(argv)))


if __name__ == "__main__":
    main()
