"""Register an existing ChatGPT Team locally via OAuth. Does not create a team at OpenAI."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.identity import ensure_membership
from app.application.resources.proxies import proxy_profile_service
from app.application.tokens import auth_service
from app.core.jwt import jwt_parser
from app.core.proxy import normalize_proxy_url
from app.domain.automation import DEFAULT_OAUTH_CLIENT_ID, OAUTH_REDIRECT_URI
from app.domain.identity import (
    AUTH_STATE_UNKNOWN,
    LOCAL_PURPOSE_MOTHER,
    MEMBERSHIP_STATE_JOINED,
    OFFICIAL_PLAN_UNKNOWN,
    OFFICIAL_ROLE_OWNER,
)
from app.domain.identity.ids import looks_like_user_id, normalize_email, workspace_official_id
from app.domain.identity.policy import normalize_official_plan, normalize_operational_state, normalize_workspace_status
from app.integrations.openai import oauth_sessions
from app.integrations.openai.chatgpt import chatgpt_client
from app.integrations.openai.oauth_sessions import parse_oauth_callback
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


def _optional_proxy(value: str | None) -> str | None:
    text = _clean(value, limit=500)
    if not text:
        return None
    try:
        return normalize_proxy_url(text)
    except ValueError as exc:
        raise RegisterWorkspaceError("代理地址格式不对") from exc


def _org_workspace_id(org: dict[str, Any]) -> str | None:
    for key in ("id", "account_id", "chatgpt_account_id", "workspace_id"):
        found = workspace_official_id(org.get(key))
        if found:
            return found
    return None


def _select_workspace(orgs: list[dict[str, Any]], chatgpt_account_id: str | None) -> dict[str, Any] | None:
    seen: dict[str, dict[str, Any]] = {}
    for org in orgs:
        oid = _org_workspace_id(org)
        if not oid:
            continue
        role = str(org.get("role") or "").lower()
        title = str(org.get("title") or org.get("name") or "").strip() or None
        owner = "owner" in role
        previous = seen.get(oid)
        if previous is None or (owner and not previous["owner"]):
            seen[oid] = {"id": oid, "title": title, "owner": owner}
    owners = [item for item in seen.values() if item["owner"]]
    chatgpt_ws = workspace_official_id(chatgpt_account_id)
    if len(owners) == 1:
        return owners[0]
    if chatgpt_ws and chatgpt_ws in seen:
        return seen[chatgpt_ws]
    if len(seen) == 1:
        return next(iter(seen.values()))
    if chatgpt_ws and not seen:
        return {"id": chatgpt_ws, "title": None, "owner": True}
    if len(owners) > 1:
        raise RegisterWorkspaceError("这个母号有多个 Team，无法自动选择 Workspace")
    if len(seen) > 1:
        raise RegisterWorkspaceError("这个母号有多个 Workspace，无法自动选择")
    return None


def identity_from_tokens(*tokens: str | None) -> dict[str, Any]:
    email = None
    user_id = None
    account_id = None
    orgs: list[dict[str, Any]] = []
    for token in tokens:
        if not token:
            continue
        email = email or jwt_parser.extract_email(token)
        user_id = user_id or jwt_parser.extract_user_id(token)
        account_id = account_id or jwt_parser.extract_chatgpt_account_id(token)
        orgs.extend(jwt_parser.extract_organizations(token))
    workspace = _select_workspace(orgs, account_id)
    official_account_id = workspace_official_id(account_id)
    if official_account_id is None and account_id and not looks_like_user_id(account_id):
        official_account_id = str(account_id).strip() or None
    return {
        "email": normalize_email(email) if email else None,
        "official_user_id": user_id,
        "official_account_id": official_account_id or (workspace["id"] if workspace else None),
        "workspace_id": workspace["id"] if workspace else None,
        "workspace_name": workspace.get("title") if workspace else None,
        "official_plan": "team" if workspace else OFFICIAL_PLAN_UNKNOWN,
    }


async def _existing_account(db: AsyncSession, email: str) -> Account | None:
    return (await db.execute(select(Account).where(Account.email == email))).scalar_one_or_none()


async def start_workspace_oauth(db: AsyncSession, *, email: str, proxy: str | None = None) -> dict[str, Any]:
    email_n = _require_email(email)
    proxy_value = _optional_proxy(proxy)
    if await _existing_account(db, email_n) is not None:
        raise RegisterWorkspaceError("这个邮箱已经在本地账号里，不能再登记成新母号", status_code=409)
    authorize = chatgpt_client.create_oauth_authorize_url(
        client_id=oauth_sessions.CLIENT_ID or DEFAULT_OAUTH_CLIENT_ID,
        redirect_uri=oauth_sessions.REDIRECT_URI or OAUTH_REDIRECT_URI,
        login_hint=email_n,
    )
    session = oauth_sessions.create_session(
        team_id=0,
        email=email_n,
        authorize=authorize,
        role="owner",
        mode="manual",
        proxy=proxy_value or "",
        password="",
        team_name="",
    )
    return {
        "ok": True,
        "ticket": session["ticket"],
        "authorize_url": session["authorize_url"],
        "redirect_uri": session.get("redirect_uri") or oauth_sessions.REDIRECT_URI,
        "email": email_n,
    }


async def register_workspace(
    db: AsyncSession,
    *,
    email: str,
    official_workspace_id: str,
    name: str | None = None,
    proxy: str | None = None,
    access_token: str | None = None,
    refresh_token: str | None = None,
    id_token: str | None = None,
    client_id: str | None = None,
    official_user_id: str | None = None,
    official_account_id: str | None = None,
    official_plan: str | None = None,
) -> dict[str, Any]:
    email_n = _require_email(email)
    workspace_id = workspace_official_id(official_workspace_id)
    if workspace_id is None:
        raise RegisterWorkspaceError("授权结果里没有官方 Workspace UUID")
    proxy_value = _optional_proxy(proxy) if proxy else None
    client = _clean(client_id, limit=100) or None
    team_name = _clean(name, limit=255) or None

    existing_account = await _existing_account(db, email_n)
    if existing_account is not None:
        raise RegisterWorkspaceError("这个邮箱已经在本地账号里，不能再登记成新母号", status_code=409)

    existing_workspace = (
        await db.execute(select(Workspace).where(Workspace.official_workspace_id == workspace_id))
    ).scalar_one_or_none()
    if existing_workspace is not None:
        raise RegisterWorkspaceError("这个 Workspace ID 已经登记过了", status_code=409)

    proxy_profile_id = None
    if proxy_value:
        profile = await proxy_profile_service.upsert_from_url(
            db,
            proxy_value,
            name=f"母号 {email_n}",
        )
        proxy_profile_id = profile.id

    account = Account(
        email=email_n,
        official_plan=normalize_official_plan(official_plan),
        official_user_id=_clean(official_user_id, limit=100) or None,
        official_account_id=_clean(official_account_id, limit=100) or workspace_id,
        auth_state=AUTH_STATE_UNKNOWN,
        operational_state=normalize_operational_state("active"),
        local_purpose=LOCAL_PURPOSE_MOTHER,
        proxy=proxy_value,
        proxy_profile_id=proxy_profile_id,
        password_encrypted=None,
        client_id=client,
    )
    db.add(account)
    await db.flush()
    await auth_service.apply_tokens(
        account,
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "client_id": client,
        },
    )
    if not access_token:
        account.auth_state = AUTH_STATE_UNKNOWN

    from app.domain.workspaces.names import apply_official_name, placeholder_name

    workspace = Workspace(
        official_workspace_id=workspace_id,
        name=None,
        official_name=None,
        custom_name=None,
        name_source="placeholder",
        subscription_plan=None,
        owner_account_id=account.id,
        status=normalize_workspace_status("active"),
        seat_limit=None,
        occupied_seats=None,
        source_team_id=None,
    )
    db.add(workspace)
    await db.flush()
    apply_official_name(workspace, team_name, owner_email=email_n)
    if not workspace.name:
        workspace.name = placeholder_name(workspace)

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


async def complete_workspace_oauth(
    db: AsyncSession,
    *,
    ticket: str,
    callback_url: str,
    client=None,
) -> dict[str, Any]:
    session = oauth_sessions.get_session(ticket)
    if session is None:
        raise RegisterWorkspaceError("认证会话不存在或已过期")
    parsed = parse_oauth_callback(callback_url)
    if parsed.get("error"):
        raise RegisterWorkspaceError(parsed.get("error_description") or parsed["error"])
    if not parsed.get("code"):
        raise RegisterWorkspaceError("回调地址里没有授权码，请把跳转到 localhost:1455 的整段地址贴回来")
    expected_state = str(session.get("state") or "")
    got_state = str(parsed.get("state") or "")
    if expected_state and got_state and got_state != expected_state:
        raise RegisterWorkspaceError("回调 state 不匹配，请重新生成授权链接")
    exchanger = client or chatgpt_client
    exchanged = await exchanger.exchange_oauth_code(
        code=parsed["code"],
        client_id=str(session.get("client_id") or DEFAULT_OAUTH_CLIENT_ID),
        redirect_uri=str(session.get("redirect_uri") or OAUTH_REDIRECT_URI),
        code_verifier=str(session.get("code_verifier") or ""),
        db_session=db,
        identifier=str(session.get("email") or "oauth_exchange"),
    )
    if not exchanged.get("success") or not exchanged.get("access_token"):
        raise RegisterWorkspaceError(str(exchanged.get("error") or "换票失败"))
    identity = identity_from_tokens(exchanged.get("access_token"), exchanged.get("id_token"))
    session_email = normalize_email(session.get("email"))
    token_email = identity.get("email")
    if token_email and token_email != session_email:
        raise RegisterWorkspaceError(f"登录邮箱是 {token_email}，和填写的 {session_email} 不一致")
    workspace_id = identity.get("workspace_id")
    if not workspace_id:
        raise RegisterWorkspaceError("授权成功，但令牌里没有 ChatGPT Team Workspace。请确认这是母号。")
    oauth_sessions.pop_session(ticket)
    return await register_workspace(
        db,
        email=session_email,
        official_workspace_id=str(workspace_id),
        name=identity.get("workspace_name"),
        proxy=str(session.get("proxy") or "") or None,
        access_token=exchanged.get("access_token"),
        refresh_token=exchanged.get("refresh_token"),
        id_token=exchanged.get("id_token"),
        client_id=str(session.get("client_id") or "") or None,
        official_user_id=identity.get("official_user_id"),
        official_account_id=identity.get("official_account_id"),
        official_plan=identity.get("official_plan"),
    )
