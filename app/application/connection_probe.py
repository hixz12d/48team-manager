"""Read-only connection checks. Probe live credentials; never persist them."""

from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.resources.hme import (
    DEFAULT_HME_BASE_URL,
    HmeConfig,
    HmeError,
    hme_client,
    load_config as load_hme_config,
    resolve_account,
)
from app.application.settings import SECRET_MASK, get_setting_value
from app.domain.resources import is_unoccupied_label
from app.integrations.sub2api.client import DEFAULT_SUB2API_BASE_URL, sub2api_client


def _text(value: Any) -> str:
    return str(value or "").strip()


def _keep_secret(value: Any) -> bool:
    text = _text(value)
    return (not text) or text == SECRET_MASK


def _error_text(exc: BaseException) -> str:
    text = _text(exc)
    return text or exc.__class__.__name__


def _failed(message: str, **extra: Any) -> dict[str, Any]:
    payload = {"ok": False, "error": message}
    payload.update(extra)
    return payload


async def _sub2api_config(db: AsyncSession, payload: dict[str, Any] | None) -> dict[str, Any]:
    stored = await sub2api_client.load_config(db)
    data = payload or {}
    base_url = _text(data.get("sub2api_base_url")) or stored.get("base_url") or DEFAULT_SUB2API_BASE_URL
    api_key = stored.get("api_key") or ""
    if not _keep_secret(data.get("sub2api_api_key")):
        api_key = _text(data.get("sub2api_api_key"))
    email = _text(data.get("sub2api_admin_email")) if "sub2api_admin_email" in data else (stored.get("email") or "")
    password = stored.get("password") or ""
    if not _keep_secret(data.get("sub2api_admin_password")):
        password = _text(data.get("sub2api_admin_password"))
    return {
        "base_url": base_url.rstrip("/"),
        "api_key": api_key,
        "email": email,
        "password": password,
        "configured": bool(api_key or (email and password)),
    }


async def _hme_config(db: AsyncSession, payload: dict[str, Any] | None) -> HmeConfig:
    stored = await load_hme_config(db)
    data = payload or {}
    token = stored.service_token
    if not _keep_secret(data.get("hme_service_token")):
        token = _text(data.get("hme_service_token"))
    account_id = stored.account_id
    if "hme_account_id" in data:
        account_id = _text(data.get("hme_account_id"))
    return HmeConfig(
        base_url=_text(data.get("hme_base_url")) or stored.base_url or DEFAULT_HME_BASE_URL,
        service_token=token,
        account_id=account_id,
    )


def _account_label(account: dict[str, Any]) -> str:
    extra = account.get("extra") if isinstance(account.get("extra"), dict) else {}
    credentials = account.get("credentials") if isinstance(account.get("credentials"), dict) else {}
    return (
        sub2api_client.account_email(account)
        or _text(extra.get("email"))
        or _text(credentials.get("email"))
        or _text(account.get("email"))
        or _text(account.get("name"))
        or f"#{sub2api_client.remote_id(account) or '?'}"
    )


def _looks_like_owner(account: dict[str, Any]) -> bool:
    blob = " ".join(
        [
            _text(account.get("name")),
            _text(account.get("role")),
            _text(account.get("type")),
            _account_label(account),
        ]
    ).lower()
    return "owner" in blob and "child" not in blob


async def probe_sub2api(db: AsyncSession, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = await _sub2api_config(db, payload)
    if not cfg["configured"]:
        return _failed("还没配 Sub2API 的 Key 或后台账号")
    try:
        groups = await sub2api_client.list_groups(db, cfg)
        accounts = await sub2api_client.list_status_accounts(db, cfg)
    except Exception as exc:  # noqa: BLE001
        return _failed(_error_text(exc))
    owners_by_group: dict[int, list[str]] = {}
    members_by_group: dict[int, list[str]] = {}
    for account in accounts:
        label = _account_label(account)
        for group_id in sub2api_client.account_group_ids(account):
            members_by_group.setdefault(group_id, []).append(label)
            if _looks_like_owner(account):
                owners_by_group.setdefault(group_id, []).append(label)
    items = []
    for group in groups:
        group_id = sub2api_client.remote_id(group)
        owners = owners_by_group.get(group_id or -1) or members_by_group.get(group_id or -1, [])
        unique_owners: list[str] = []
        seen: set[str] = set()
        for email in owners:
            key = email.lower()
            if key in seen:
                continue
            seen.add(key)
            unique_owners.append(email)
        items.append(
            {
                "id": group_id,
                "name": _text(group.get("name")) or f"分组 {group_id or '?'}",
                "account_count": group.get("account_count") or group.get("accounts_count") or len(owners),
                "owners": unique_owners[:8],
            }
        )
    return {"ok": True, "group_count": len(items), "account_count": len(accounts), "groups": items}


async def probe_hme(db: AsyncSession, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = await _hme_config(db, payload)
    if not cfg.configured:
        return _failed("还没配 HME Token")
    try:
        accounts = hme_client.list_accounts(cfg)
        account = resolve_account(accounts, cfg.account_id)
        account_id = _text(account.get("id"))
        aliases = hme_client.list_aliases(cfg, account_id)
    except HmeError as exc:
        return _failed(str(exc))
    except Exception as exc:  # noqa: BLE001
        return _failed(_error_text(exc))
    active = [item for item in aliases if item.get("active")]
    unused = [item for item in active if is_unoccupied_label(item.get("label") or "")]
    return {
        "ok": True,
        "account_id": account_id,
        "account_name": _text(account.get("name") or account.get("email") or account_id),
        "alias_count": len(aliases),
        "active_count": len(active),
        "unused_count": len(unused),
    }


async def probe_mail(db: AsyncSession, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.integrations.mail.cloudflare import (
        CF_SETTING_ADDRESS,
        CF_SETTING_ADMIN_PASSWORD,
        CF_SETTING_BASE_URL,
        DEFAULT_CF_MAIL_ADDRESS,
        DEFAULT_CF_MAIL_BASE_URL,
        cloudflare_mail_client,
        normalize_cloudflare_base_url,
        normalize_mailbox_address,
    )

    data = payload or {}
    try:
        base_url = normalize_cloudflare_base_url(
            _text(data.get("cf_mail_base_url"))
            or (await get_setting_value(db, CF_SETTING_BASE_URL, DEFAULT_CF_MAIL_BASE_URL) or DEFAULT_CF_MAIL_BASE_URL)
        )
        address = normalize_mailbox_address(
            _text(data.get("cf_mail_address"))
            or (await get_setting_value(db, CF_SETTING_ADDRESS, DEFAULT_CF_MAIL_ADDRESS) or DEFAULT_CF_MAIL_ADDRESS)
        )
    except ValueError as exc:
        return _failed(str(exc))
    password = await get_setting_value(db, CF_SETTING_ADMIN_PASSWORD, "") or ""
    if not _keep_secret(data.get("cf_mail_admin_password")):
        password = _text(data.get("cf_mail_admin_password"))
    if not password:
        return _failed("还没配临时邮箱密码")
    try:
        messages = cloudflare_mail_client.fetch_messages(
            base_url=base_url,
            address=address,
            admin_password=password,
            limit=1,
        )
    except Exception as exc:  # noqa: BLE001
        return _failed(_error_text(exc))
    return {"ok": True, "address": address, "reachable": True, "sample_count": len(messages)}
