"""Direct codex-rs OAuth handoff for the personal signup extension (no workspace lookup/sync).

Starts a desktop OAuth session with a healthy codex-rs proxy (or an existing rs account's own
proxy), exchanges the callback strictly through that proxy, verifies a Team workspace plan, and
imports or updates the account in codex-rs while preserving existing settings on updates.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.codex_export import CodexTransferError
from app.application.codex_publish import _token_identity, _verify_remote
from app.application.codex_reauth import FrozenProxyClient
from app.application.codex_rs_onboard import TEAM_PLAN_PREFIXES, TEAM_PLAN_TYPES, _pick_proxy
from app.application.codex_rs_recovery import _Stop, _restore_settings
from app.application.oauth_sessions import OAuthSessionError, oauth_session_store
from app.application.settings import load_codex_rs_import_defaults
from app.core.jwt import jwt_parser
from app.core.proxy import normalize_proxy_url
from app.domain.identity.ids import normalize_email
from app.integrations.codex.client import CodexClient, account_group_ids, load_config
from app.integrations.openai import oauth_sessions
from app.integrations.openai.chatgpt import chatgpt_client

logger = logging.getLogger(__name__)

PURPOSE = "extension_rs"
EXCHANGE_ID = "extension_rs"
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


def _err(error_code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "state": "failed", "error_code": error_code, "message": message, **extra}


async def _load_client(db: AsyncSession) -> CodexClient:
    cfg = await load_config(db)
    await db.commit()
    return CodexClient(cfg["base_url"], cfg["api_key"])


async def start_extension_rs_auth(db: AsyncSession, *, email: str) -> dict[str, Any]:
    """Allocate a codex-rs proxy and create a desktop OAuth session without querying Team48 workspaces."""
    target = normalize_email(email)
    if not target or len(target) > 254 or not _EMAIL_RE.match(target):
        return _err("email_required", "邮箱格式不正确")

    try:
        client = await _load_client(db)
    except (CodexTransferError, ValueError) as exc:
        await db.rollback()
        return _err("codex_not_configured", str(exc) or "请先在 48team 设置页配置 codex-rs 地址和管理员 API Key")

    try:
        accounts = await client.list_accounts()
    except CodexTransferError as exc:
        return _err(exc.code, str(exc))

    matches = [acc for acc in accounts if isinstance(acc, dict) and normalize_email(acc.get("email")) == target]
    if len(matches) > 1:
        return _err("multiple_rs_accounts", "codex-rs 中已有多个该邮箱的账号，请先在 rs 后台处理重复项后再接入")

    existing = len(matches) == 1
    proxy_id: str | None = None
    proxy_url: str | None = None
    if existing:
        remote_id = str(matches[0].get("id") or "").strip()
        if remote_id:
            try:
                detail = await client.detail(remote_id)
                raw_proxy = await client.export_proxy_url(remote_id)
                proxy_url = normalize_proxy_url(raw_proxy)
                raw_id = detail.get("outboundProxyId")
                proxy_id = str(raw_id).strip() if isinstance(raw_id, str) and raw_id.strip() else None
            except (CodexTransferError, ValueError):
                proxy_url = None
                proxy_id = None

    if not proxy_url:
        try:
            proxy_id, proxy_url = await _pick_proxy(client)
        except _Stop as stop:
            if stop.code == "no_proxy":
                return _err("no_proxy", "codex-rs 没有可用代理（需启用、测试通过且至少已挂 1 个号用于读取代理地址）")
            return _err(stop.code, "无法从 codex-rs 获取可用代理")

    try:
        auth = chatgpt_client.create_oauth_authorize_url(
            oauth_sessions.CLIENT_ID, oauth_sessions.REDIRECT_URI, login_hint=target, entry="desktop"
        )
    except Exception as exc:  # noqa: BLE001
        return _err("oauth_start_failed", f"生成授权链接失败：{exc}")

    session = oauth_sessions.create_session(
        team_id=0,
        email=target,
        authorize=auth,
        role="child",
        mode="manual",
        proxy=proxy_url,
        proxy_source="codex_rs",
        proxy_instance_key=(proxy_id or "")[:64],
    )
    stored_session = oauth_sessions.get_session(session["ticket"])
    if stored_session is None:
        return _err("oauth_session_invalid", "无法保存 OAuth 会话")

    try:
        await oauth_session_store.persist(db, stored_session, purpose=PURPOSE)
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        oauth_sessions.pop_session(session["ticket"])
        return _err("oauth_start_failed", f"保存授权会话失败：{exc}")

    return {
        "ok": True,
        "state": "authorize",
        "rs_direct": True,
        "existing": existing,
        "email": target,
        "ticket": session["ticket"],
        "authorize_url": session.get("authorize_url") or auth.get("authorize_url") or "",
        "message": "rs 授权链接已生成",
    }


async def complete_extension_rs_auth(db: AsyncSession, *, ticket: str, callback_url: str) -> dict[str, Any]:
    """Exchange the OAuth callback via the session's rs proxy, verify Team plan, and import/update codex-rs."""
    try:
        stored, parsed = await oauth_session_store.begin_exchange(db, ticket, callback_url, purpose=PURPOSE)
    except OAuthSessionError as exc:
        await db.rollback()
        return _err(exc.error_code, str(exc))

    async def fail_session(code: str, message: str) -> dict[str, Any]:
        await oauth_session_store.finish(db, stored, success=False)
        oauth_sessions.pop_session(ticket)
        await db.commit()
        return _err(code, message)

    context = oauth_session_store.exchange_context(stored)
    email = normalize_email(stored.email)
    try:
        proxy = normalize_proxy_url(context["proxy"])
    except ValueError:
        proxy = None
    if not proxy:
        return await fail_session("oauth_proxy_missing", "授权会话缺少代理快照，请重新发起接入")

    picked_proxy_id = str(stored.proxy_instance_key or "").strip() or None

    exchanger = FrozenProxyClient(proxy)
    try:
        exchanged = await exchanger.exchange_oauth_code(
            parsed["code"],
            context["client_id"] or oauth_sessions.CLIENT_ID,
            context["redirect_uri"] or oauth_sessions.REDIRECT_URI,
            context["code_verifier"],
            db,
            identifier=EXCHANGE_ID,
        )
        await db.commit()
    finally:
        await exchanger.clear_session(EXCHANGE_ID)
    parsed.clear()

    if not exchanged.get("success") or not exchanged.get("access_token"):
        code = str(exchanged.get("error_code") or "oauth_exchange_failed")
        msg = str(exchanged.get("error") or "换取令牌失败")
        exchanged.clear()
        return await fail_session(code, msg)

    access_token = str(exchanged.get("access_token") or "")
    refresh_token = str(exchanged.get("refresh_token") or "")
    id_token = str(exchanged.get("id_token") or "") or None
    exchanged.clear()

    identity = _token_identity(access_token) or {}
    auth_claim = jwt_parser.extract_auth_claim(access_token)
    plan_type = str(auth_claim.get("chatgpt_plan_type") or "").strip().lower()
    if not refresh_token or identity.get("email") != email:
        access_token = refresh_token = ""
        id_token = None
        return await fail_session("token_identity_mismatch", f"授权邮箱与任务邮箱 {email} 不一致，或缺少刷新令牌")

    if not identity.get("account_id") or not (
        plan_type in TEAM_PLAN_TYPES or plan_type.startswith(TEAM_PLAN_PREFIXES)
    ):
        access_token = refresh_token = ""
        id_token = None
        logger.info("extension rs rejected non-team plan email=%s*** plan_type=%s", email[:3], plan_type or "-")
        return await fail_session("not_team_workspace", "授权到的不是 Team 工作空间（当前为个人空间），未导入 codex-rs")

    try:
        defaults = await load_codex_rs_import_defaults(db)
        client = await _load_client(db)
    except (CodexTransferError, ValueError) as exc:
        access_token = refresh_token = ""
        id_token = None
        await db.rollback()
        return await fail_session("codex_not_configured", str(exc) or "48team 未配置 codex-rs")

    document: dict[str, Any] = {
        "provider": "openai",
        "email": identity["email"],
        "access_token": access_token,
        "refresh_token": refresh_token,
        "client_id": oauth_sessions.CLIENT_ID,
    }
    if id_token:
        document["id_token"] = id_token
    access_token = refresh_token = ""
    id_token = None

    try:
        accounts = await client.list_accounts()
        exact = next(
            (
                acc for acc in accounts
                if isinstance(acc, dict)
                and normalize_email(acc.get("email")) == email
                and acc.get("accountId") == identity["account_id"]
                and acc.get("userId") == identity["user_id"]
            ),
            None,
        )
        email_matches = [acc for acc in accounts if isinstance(acc, dict) and normalize_email(acc.get("email")) == email]
        if exact is None and len(email_matches) > 1:
            document.clear()
            return await fail_session("multiple_rs_accounts", "codex-rs 中已有多个该邮箱账号，未覆盖写入")

        target_existing = exact or (email_matches[0] if len(email_matches) == 1 else None)
        before = await client.detail(str(target_existing["id"])) if target_existing and target_existing.get("id") else None

        if before is not None:
            same_identity = (
                before.get("accountId") == identity["account_id"]
                and before.get("userId") == identity["user_id"]
            )
            if same_identity:
                imported_ids = await client.import_accounts(document)
                remote_id = imported_ids[0]
                await _restore_settings(client, remote_id, before, proxy)
            else:
                preserved_settings = {
                    "enabled": bool(before.get("enabled", True)),
                    "concurrencyLimit": before.get("concurrencyLimit") if type(before.get("concurrencyLimit")) is int else None,
                    "weight": int(before.get("weight") or 1),
                    "groupIds": account_group_ids(before),
                }
                existing_proxy_id = str(before.get("outboundProxyId") or "").strip() or picked_proxy_id
                if not existing_proxy_id:
                    document["outboundProxyUrl"] = proxy
                imported_ids = await client.import_accounts(
                    document, raw_settings=preserved_settings, proxy_id=existing_proxy_id
                )
                remote_id = imported_ids[0]
        else:
            proxy_id = picked_proxy_id
            if not proxy_id:
                proxy_id, _ = await _pick_proxy(client)
            imported_ids = await client.import_accounts(document, settings=defaults, proxy_id=proxy_id)
            remote_id = imported_ids[0]

        remote = await client.detail(remote_id)
        _verify_remote(remote, identity)
    except _Stop as stop:
        return await fail_session(stop.code, "导入 codex-rs 后核对或恢复原设置失败，请到 rs 后台检查")
    except CodexTransferError as exc:
        return await fail_session(exc.code, str(exc))
    finally:
        document.clear()

    await oauth_session_store.finish(db, stored, success=True)
    oauth_sessions.pop_session(ticket)
    await db.commit()

    updated = before is not None
    summary = "已更新 codex-rs 账号凭据（保留原有分组/代理/并发）" if updated else "已授权并导入 codex-rs"
    detail_msg = "已覆盖更新凭据，保留原设置" if updated else "已导入 codex-rs，续期由 codex-rs 负责"
    return {
        "ok": True,
        "email": identity["email"],
        "remote_account_id": remote_id,
        "updated_existing": updated,
        "message": summary,
        "followups": {
            "codex_rs": {
                "ok": True,
                "remote_account_id": remote_id,
                "message": detail_msg,
            }
        },
    }
