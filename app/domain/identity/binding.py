"""Explicit Sub2API binding. Names and family prefixes never match accounts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.domain.identity import BINDING_CONFLICT, BINDING_PENDING, BINDING_VERIFIED, PROVIDER_SUB2API
from app.domain.identity.ids import is_workspace_account_id, normalize_email


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


def expected_workspace_id(
    account,
    *,
    memberships: Sequence[Any],
    workspaces_by_id: Mapping[int, Any],
) -> str | None:
    owned = [
        workspace
        for workspace in workspaces_by_id.values()
        if workspace.owner_account_id == account.id
        and workspace.official_workspace_id
        and is_workspace_account_id(workspace.official_workspace_id)
    ]
    if owned:
        return owned[0].official_workspace_id
    for row in memberships:
        workspace = workspaces_by_id.get(row.workspace_id)
        if (
            workspace
            and workspace.official_workspace_id
            and is_workspace_account_id(workspace.official_workspace_id)
        ):
            return workspace.official_workspace_id
    return None


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
