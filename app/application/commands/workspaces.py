"""Register an existing ChatGPT Team locally. Does not create a team at OpenAI."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import ensure_membership
from app.application.tokens import encrypt_secret
from app.domain.identity import (
    AUTH_STATE_UNKNOWN,
    LOCAL_PURPOSE_MOTHER,
    MEMBERSHIP_STATE_JOINED,
    OFFICIAL_PLAN_UNKNOWN,
    OFFICIAL_ROLE_OWNER,
)
from app.domain.identity.ids import normalize_email, workspace_official_id
from app.domain.identity.policy import normalize_operational_state, normalize_workspace_status
from app.persistence.models.identity import Account, Workspace


class RegisterWorkspaceError(ValueError):
    def __init__(self, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _clean(value: str | None, *, limit: int | None = None) -> str:
    text = str(value or "").strip()
    if limit is not None:
        return text[:limit]
    return text


def _require_email(value: str | None) -> str:
    email = normalize_email(value)
    if "@" not in email or email.startswith("@") or email.endswith("@") or "." not in email.rsplit("@", 1)[-1]:
        raise RegisterWorkspaceError("母号邮箱格式不对")
    return email


async def register_workspace(
    db: AsyncSession,
    *,
    email: str,
    official_workspace_id: str,
    name: str | None = None,
    seat_limit: int | None = None,
    proxy: str | None = None,
    password: str | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    session_token: str | None = None,
    id_token: str | None = None,
    client_id: str | None = None,
) -> dict[str, Any]:
    email_n = _require_email(email)
    workspace_id = workspace_official_id(official_workspace_id)
    if workspace_id is None:
        raise RegisterWorkspaceError("Workspace ID 必须是官方 Workspace UUID，不能填 user-xxx")
    team_name = _clean(name, limit=255) or None
    proxy_value = _clean(proxy, limit=500) or None
    client = _clean(client_id, limit=100) or None

    existing_account = (await db.execute(select(Account).where(Account.email == email_n))).scalar_one_or_none()
    if existing_account is not None:
        raise RegisterWorkspaceError("这个邮箱已经在本地账号里，不能再登记成新母号", status_code=409)

    existing_workspace = (
        await db.execute(select(Workspace).where(Workspace.official_workspace_id == workspace_id))
    ).scalar_one_or_none()
    if existing_workspace is not None:
        raise RegisterWorkspaceError("这个 Workspace ID 已经登记过了", status_code=409)

    account = Account(
        email=email_n,
        official_plan=OFFICIAL_PLAN_UNKNOWN,
        official_user_id=None,
        official_account_id=None,
        auth_state=AUTH_STATE_UNKNOWN,
        operational_state=normalize_operational_state("active"),
        local_purpose=LOCAL_PURPOSE_MOTHER,
        proxy=proxy_value,
        password_encrypted=encrypt_secret(password),
        access_token_encrypted=encrypt_secret(access_token),
        refresh_token_encrypted=encrypt_secret(refresh_token),
        session_token_encrypted=encrypt_secret(session_token),
        id_token_encrypted=encrypt_secret(id_token),
        client_id=client,
    )
    db.add(account)
    await db.flush()

    workspace = Workspace(
        official_workspace_id=workspace_id,
        name=team_name or email_n,
        subscription_plan=None,
        owner_account_id=account.id,
        status=normalize_workspace_status("active"),
        seat_limit=seat_limit,
        source_team_id=None,
    )
    db.add(workspace)
    await db.flush()

    await ensure_membership(
        db,
        workspace_id=workspace.id,
        account_id=account.id,
        official_role=OFFICIAL_ROLE_OWNER,
        membership_state=MEMBERSHIP_STATE_JOINED,
        local_purpose=LOCAL_PURPOSE_MOTHER,
    )
    await db.commit()
    await db.refresh(account)
    await db.refresh(workspace)
    return {
        "ok": True,
        "workspace": {
            "id": workspace.id,
            "name": workspace.name,
            "official_workspace_id": workspace.official_workspace_id,
            "owner_email": account.email,
            "members": 0,
            "seat_limit": workspace.seat_limit,
            "status": workspace.status,
        },
        "account": {
            "id": account.id,
            "email": account.email,
            "purpose": account.local_purpose,
        },
    }
