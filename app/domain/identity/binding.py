"""Explicit Sub2API binding. Names and family prefixes never match accounts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.identity import (
    BINDING_CONFLICT,
    BINDING_PENDING,
    BINDING_VERIFIED,
    MANAGEMENT_ROLE_MOTHER,
    PROVIDER_SUB2API,
)
from app.domain.identity.ids import is_workspace_account_id, normalize_email
from app.domain.identity.policy import management_role


def remote_id_from(account: Mapping[str, Any]) -> str:
    value = account.get("id")
    if value in (None, "", 0, "0"):
        return ""
    return str(value).strip()


def remote_email_from(account: Mapping[str, Any]) -> str:
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    for source in (account, credentials, extra):
        if not isinstance(source, dict):
            continue
        for key in ("email", "chatgpt_email", "official_email"):
            email = normalize_email(str(source.get(key) or ""))
            if email:
                return email
    return ""


def remote_official_account_id_from(account: Mapping[str, Any]) -> str:
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    for source in (account, credentials, extra):
        if not isinstance(source, dict):
            continue
        for key in ("chatgpt_account_id", "official_account_id", "account_id"):
            value = str(source.get(key) or "").strip()
            if value and not value.lower().startswith("user-"):
                return value
    return ""


def remote_workspace_id_from(account: Mapping[str, Any]) -> str | None:
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    for source in (account, credentials, extra):
        if not isinstance(source, dict):
            continue
        for key in ("workspace_id", "chatgpt_workspace_id", "official_workspace_id"):
            value = str(source.get(key) or "").strip()
            if value and is_workspace_account_id(value):
                return value.lower()
    return None


def remote_snapshot(account: Mapping[str, Any]) -> dict[str, str | None]:
    return {
        "remote_account_id": remote_id_from(account),
        "email": remote_email_from(account) or None,
        "official_account_id": remote_official_account_id_from(account) or None,
        "workspace_id": remote_workspace_id_from(account),
        "name": str(account.get("name") or "") or None,
    }


def email_local_part(email: str | None) -> str:
    normalized = normalize_email(email)
    if "@" not in normalized:
        return normalized
    return normalized.split("@", 1)[0]


def canonical_sub2api_name(email: str, context_role: str, *, team_email: str | None = None) -> str:
    suffix = "母号" if context_role == MANAGEMENT_ROLE_MOTHER else "子号"
    label_source = team_email if context_role != MANAGEMENT_ROLE_MOTHER and team_email else email
    local_part = email_local_part(label_source)
    return f"Team（{local_part}） {suffix}"


def team48_context_key(official_workspace_id: str | None, account_id: int) -> str:
    workspace = str(official_workspace_id or "").strip().lower()
    return f"team48:{workspace}:{int(account_id)}"


def remote_context_key_from(account: Mapping[str, Any]) -> str:
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    for source in (account, extra, credentials):
        if not isinstance(source, dict):
            continue
        value = str(source.get("team48_context_key") or "").strip()
        if value:
            return value
    return ""


class AmbiguousWorkspaceContext(ValueError):
    error_code = "ambiguous_workspace_context"

    def __init__(self, message: str = "account belongs to multiple workspaces; workspace_id is required"):
        super().__init__(message)


def workspace_contexts(
    account,
    *,
    memberships: Sequence[Any],
    workspaces_by_id: Mapping[int, Any],
) -> list[Any]:
    seen: set[int] = set()
    contexts: list[Any] = []
    for workspace in workspaces_by_id.values():
        if workspace.owner_account_id == account.id and workspace.id not in seen:
            seen.add(workspace.id)
            contexts.append(workspace)
    for row in memberships:
        workspace = workspaces_by_id.get(row.workspace_id)
        if workspace is None or workspace.id in seen:
            continue
        seen.add(workspace.id)
        contexts.append(workspace)
    return contexts


def resolve_workspace_context(
    account,
    *,
    memberships: Sequence[Any],
    workspaces_by_id: Mapping[int, Any],
    workspace_id: int | None = None,
):
    contexts = workspace_contexts(account, memberships=memberships, workspaces_by_id=workspaces_by_id)
    if workspace_id is not None:
        workspace = workspaces_by_id.get(int(workspace_id))
        if workspace is None:
            raise ValueError("workspace not found")
        if workspace not in contexts and workspace.owner_account_id != account.id:
            if all(row.workspace_id != workspace.id for row in memberships):
                raise ValueError("account is not in the requested workspace")
        return workspace
    if len(contexts) == 1:
        return contexts[0]
    if not contexts:
        return None
    raise AmbiguousWorkspaceContext()


def expected_workspace_id(
    account,
    *,
    memberships: Sequence[Any],
    workspaces_by_id: Mapping[int, Any],
    workspace_id: int | None = None,
    allow_ambiguous: bool = False,
) -> str | None:
    try:
        workspace = resolve_workspace_context(
            account,
            memberships=memberships,
            workspaces_by_id=workspaces_by_id,
            workspace_id=workspace_id,
        )
    except AmbiguousWorkspaceContext:
        if allow_ambiguous:
            return None
        raise
    if workspace is None:
        return None
    official = str(workspace.official_workspace_id or "").strip()
    if official and is_workspace_account_id(official):
        return official.lower()
    return None


def canonical_name_for_account(account, workspace, *, owner_email: str | None = None) -> str:
    role = management_role(workspace, account.id)
    team_email = owner_email
    if not team_email and workspace is not None:
        owner = getattr(workspace, "owner_account", None)
        team_email = getattr(owner, "email", None) if owner is not None else None
    return canonical_sub2api_name(account.email, role, team_email=team_email)


def match_unbound_local_account(
    remote: Mapping[str, Any],
    *,
    accounts_by_email: Mapping[str, Any],
    accounts_by_official: Mapping[str, list[Any]],
) -> tuple[Any | None, str | None]:
    official_id = remote_official_account_id_from(remote)
    if official_id:
        matches = accounts_by_official.get(official_id) or []
        if len(matches) > 1:
            return None, f"official account id {official_id} matches multiple local accounts"
        if len(matches) == 1:
            return matches[0], None
    email = remote_email_from(remote)
    if email:
        account = accounts_by_email.get(email)
        if account is not None:
            return account, None
    return None, "no official id or exact email match"


def cross_check_binding(
    *,
    local_email: str,
    local_official_account_id: str | None,
    expected_workspace: str | None,
    remote: Mapping[str, Any],
) -> tuple[str, str | None]:
    remote_email = remote_email_from(remote)
    local_email_n = normalize_email(local_email)
    remote_official = remote_official_account_id_from(remote)
    local_official = str(local_official_account_id or "").strip()
    remote_workspace = remote_workspace_id_from(remote)

    mismatches: list[str] = []
    if remote_email and local_email_n and remote_email != local_email_n:
        mismatches.append(f"email mismatch remote={remote_email} local={local_email_n}")
    if remote_official and local_official and remote_official != local_official:
        mismatches.append(
            f"official account id mismatch remote={remote_official} local={local_official}"
        )
    if expected_workspace and remote_workspace and remote_workspace != expected_workspace:
        mismatches.append(
            f"workspace id mismatch remote={remote_workspace} local={expected_workspace}"
        )
    if mismatches:
        return BINDING_CONFLICT, "; ".join(mismatches)

    email_confirmed = bool(remote_email and local_email_n and remote_email == local_email_n)
    official_confirmed = bool(remote_official and local_official and remote_official == local_official)
    if not email_confirmed and not official_confirmed:
        return BINDING_PENDING, "email/official id not confirmed; not promoting to verified"
    return BINDING_VERIFIED, None


def serialize_binding(binding) -> dict[str, Any]:
    return {
        "id": binding.id,
        "provider": binding.provider or PROVIDER_SUB2API,
        "local_account_id": binding.local_account_id,
        "remote_account_id": binding.remote_account_id,
        "binding_state": binding.binding_state,
        "verified_email": binding.verified_email,
        "verified_official_account_id": binding.verified_official_account_id,
        "verified_workspace_id": binding.verified_workspace_id,
        "last_observed_at": binding.last_observed_at,
        "last_error": binding.last_error,
    }
