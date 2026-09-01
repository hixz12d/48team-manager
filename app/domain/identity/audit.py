"""Read-only identity audit. Conflict blocks later automation."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from typing import Any

from app.domain.identity import (
    AUDIT_CONFLICT,
    AUDIT_SUSPICIOUS,
    AUDIT_UNBOUND,
    AUDIT_VERIFIED,
    BINDING_CONFLICT,
    BINDING_MISSING,
    BINDING_PENDING,
    BINDING_VERIFIED,
    LOCAL_PURPOSE_CHILD,
    LOCAL_PURPOSE_DISABLED,
    LOCAL_PURPOSE_FREE,
    LOCAL_PURPOSE_MOTHER,
    LOCAL_PURPOSE_STANDBY,
    MEMBERSHIP_STATE_INVITED,
    MEMBERSHIP_STATE_JOINED,
    MEMBERSHIP_STATE_UNKNOWN,
    OFFICIAL_ROLE_OWNER,
)
from app.domain.identity.ids import is_workspace_account_id, looks_like_gmail, looks_like_user_id


def audit_account(
    account,
    *,
    memberships: Sequence[Any],
    bindings: Sequence[Any],
    remote_owners: dict[tuple[str, str], list[Any]],
    workspaces_by_id: dict[int, Any],
) -> dict[str, Any]:
    reasons: list[str] = []
    result = AUDIT_VERIFIED
    owner_memberships = [
        row
        for row in memberships
        if row.official_role == OFFICIAL_ROLE_OWNER
        and row.membership_state in (MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_UNKNOWN)
    ]
    active_memberships = [
        row
        for row in memberships
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
        if (
            workspace.owner_account_id == account.id
            and workspace.official_workspace_id
            and not is_workspace_account_id(workspace.official_workspace_id)
        ):
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
        elif account.local_purpose in {LOCAL_PURPOSE_STANDBY, LOCAL_PURPOSE_FREE, LOCAL_PURPOSE_DISABLED} and not bindings:
            result = AUDIT_UNBOUND
            reasons.append("没有 Sub2API binding")
        elif not bindings:
            result = AUDIT_UNBOUND
            reasons.append("没有 Sub2API binding")
        if result == AUDIT_VERIFIED:
            if any(binding.binding_state == BINDING_VERIFIED for binding in bindings):
                reasons.append("Sub2API binding 已交叉验证")
            if any(binding.binding_state == BINDING_PENDING for binding in bindings):
                reasons.append("Sub2API binding 仍是 pending，尚未 email / official id 交叉验证")
            if any(binding.binding_state == BINDING_MISSING for binding in bindings):
                reasons.append("Sub2API remote 快照里找不到这个 remote id")

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


def build_audit_report(accounts, memberships, bindings, workspaces) -> dict[str, Any]:
    memberships_by_account: dict[int, list[Any]] = defaultdict(list)
    for row in memberships:
        memberships_by_account[row.account_id].append(row)
    bindings_by_account: dict[int, list[Any]] = defaultdict(list)
    remote_owners: dict[tuple[str, str], list[Any]] = defaultdict(list)
    for row in bindings:
        bindings_by_account[row.local_account_id].append(row)
        remote_owners[(row.provider, row.remote_account_id)].append(row)
    workspaces_by_id = {row.id: row for row in workspaces}

    findings = [
        audit_account(
            account,
            memberships=memberships_by_account.get(account.id, []),
            bindings=bindings_by_account.get(account.id, []),
            remote_owners=remote_owners,
            workspaces_by_id=workspaces_by_id,
        )
        for account in accounts
    ]
    counts = {
        AUDIT_VERIFIED: 0,
        AUDIT_CONFLICT: 0,
        AUDIT_UNBOUND: 0,
        AUDIT_SUSPICIOUS: 0,
    }
    for item in findings:
        counts[item["result"]] = counts.get(item["result"], 0) + 1
    return {"counts": counts, "findings": findings}


def format_audit_text(report: dict[str, Any]) -> str:
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
