"""SystemSetting helpers. Query payloads never include raw secrets."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import change_admin_password
from app.core.config import load_settings
from app.persistence.models.settings import SystemSetting

TRUTHY = {"1", "true", "yes", "on"}
SECRET_MASK = "••••••"


async def get_setting_value(db: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == key).execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None or row.value is None:
        return default
    return row.value


async def upsert_setting(db: AsyncSession, key: str, value: str, description: str | None = None) -> SystemSetting:
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == key).execution_options(populate_existing=True))).scalar_one_or_none()
    if row is None:
        row = SystemSetting(key=key, value=value, description=description)
        db.add(row)
    else:
        row.value = value
        if description is not None:
            row.description = description
    await db.flush()
    return row


def as_bool(value, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in TRUTHY


def _secret_view(value: str | None) -> str:
    return ""


def _secret_state(value: str | None) -> str:
    return "stored" if str(value or "").strip() else "missing"


def _keep_secret(value: str | None) -> bool:
    text = str(value or "").strip()
    return (not text) or text == SECRET_MASK


AUTH_PUSH_TARGET_KEY = "auth_push_target"
CODEX_RS_DEFAULTS_KEY = "codex_rs_import_defaults"
AUTH_PUSH_TARGETS = ("sub2api", "codex_rs")


def _default_codex_rs_import() -> dict[str, Any]:
    return {"group_ids": [], "concurrency_limit": None, "weight": 1, "enabled": True}


async def load_auth_push_target(db: AsyncSession) -> str:
    """Where account-page reauth pushes after success: "sub2api" (default) or "codex_rs"."""
    value = (await get_setting_value(db, AUTH_PUSH_TARGET_KEY, "") or "").strip()
    return value if value in AUTH_PUSH_TARGETS else "sub2api"


async def load_codex_rs_import_defaults(db: AsyncSession) -> dict[str, Any]:
    """codex-rs first-import settings; malformed stored values fall back field by field."""
    result = _default_codex_rs_import()
    raw = await get_setting_value(db, CODEX_RS_DEFAULTS_KEY)
    try:
        stored = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        stored = {}
    if not isinstance(stored, dict):
        stored = {}
    groups = stored.get("group_ids")
    if isinstance(groups, list):
        result["group_ids"] = list(dict.fromkeys(str(item) for item in groups if str(item or "").strip()))
    limit = stored.get("concurrency_limit")
    if type(limit) is int and 1 <= limit <= 4294967295:
        result["concurrency_limit"] = limit
    weight = stored.get("weight")
    if type(weight) is int and 1 <= weight <= 100:
        result["weight"] = weight
    if isinstance(stored.get("enabled"), bool):
        result["enabled"] = stored["enabled"]
    return result


async def load_codex_rs_settings(db: AsyncSession) -> dict[str, Any]:
    from app.application import codex_refill

    return {
        "push_target": await load_auth_push_target(db),
        **(await load_codex_rs_import_defaults(db)),
        "refill": await codex_refill.load_config(db),
    }


async def save_codex_rs_settings(db: AsyncSession, patch) -> None:
    """Write only the fields present in ``patch`` (a ``CodexRsSettings``)."""
    if patch.push_target is not None:
        await upsert_setting(db, AUTH_PUSH_TARGET_KEY, patch.push_target, "授权后推送去向")
    changed = False
    current = await load_codex_rs_import_defaults(db)
    if patch.group_ids is not None:
        current["group_ids"] = list(dict.fromkeys(item.strip() for item in patch.group_ids if item.strip()))
        changed = True
    if patch.concurrency_inherit:
        current["concurrency_limit"] = None
        changed = True
    elif patch.concurrency_limit is not None:
        current["concurrency_limit"] = int(patch.concurrency_limit)
        changed = True
    if patch.weight is not None:
        current["weight"] = int(patch.weight)
        changed = True
    if patch.enabled is not None:
        current["enabled"] = bool(patch.enabled)
        changed = True
    if changed:
        await upsert_setting(db, CODEX_RS_DEFAULTS_KEY, json.dumps(current), "codex-rs 首次导入默认值")
    if patch.refill is not None:
        from app.application import codex_refill

        was_enabled = (await codex_refill.load_config(db))["enabled"]
        await codex_refill.save_config(db, patch.refill.model_dump())
        if patch.refill.enabled and not was_enabled:
            await codex_refill.clear_pause(db)


async def load_console_settings(db: AsyncSession) -> dict[str, Any]:
    from app.application.resources.hme import DEFAULT_HME_BASE_URL, load_config as load_hme_config
    from app.application.resources.phones import phone_pool_service
    from app.integrations.mail.cloudflare import (
        CF_SETTING_ADDRESS,
        CF_SETTING_ADMIN_PASSWORD,
        CF_SETTING_BASE_URL,
        DEFAULT_CF_MAIL_ADDRESS,
        DEFAULT_CF_MAIL_BASE_URL,
    )
    from app.integrations.sub2api.client import DEFAULT_SUB2API_BASE_URL, sub2api_client

    from app.application.sub2api_defaults import load_defaults

    push_defaults = await load_defaults(db)
    env = load_settings()
    hme_cfg = await load_hme_config(db)
    sub2api_cfg = await sub2api_client.load_config(db)
    from app.integrations.openai.member_adapter import (
        SETTING_INVITE_SEAT_WIRE_PREMIUM,
        SETTING_INVITE_SEAT_WIRE_STANDARD,
        load_verified_seat_wire_values,
    )
    invite_wires = await load_verified_seat_wire_values(db)
    invite_seat_wire = {
        "premium": (await get_setting_value(db, SETTING_INVITE_SEAT_WIRE_PREMIUM, "") or "").strip() or None,
        "standard": (await get_setting_value(db, SETTING_INVITE_SEAT_WIRE_STANDARD, "") or "").strip() or None,
        "loaded_count": len(invite_wires),
        "workspace_default": "omit_seat_type",
    }
    phones_cfg = await phone_pool_service.get_config(db)
    from app.application.reauth import reauth_service

    reauth_cfg = await reauth_service.load_settings(db)
    from app.application.rotate import rotate_service
    rotate_cfg = await rotate_service.load_settings(db)
    from app.persistence.models.identity import Workspace
    rotation_workspaces = [{"id": row.id, "name": row.name or f"Workspace {row.id}", "status": row.status}
                           for row in await db.scalars(select(Workspace).order_by(Workspace.id))]
    from app.application.quota import quota_service
    quota_runtime = await quota_service.runtime_summary(db)
    stored_quota = quota_runtime["effective_enabled"]
    mail_password = await get_setting_value(db, CF_SETTING_ADMIN_PASSWORD, "") or ""
    mail_configured = bool(mail_password)
    codex_url = await get_setting_value(db, "codex_base_url", "") or ""
    codex_key = await get_setting_value(db, "codex_admin_key_encrypted", "") or ""
    secret_state = {
        "codex_admin_key": _secret_state(codex_key),
        "sub2api_api_key": _secret_state(sub2api_cfg.get("api_key")),
        "sub2api_admin_password": _secret_state(sub2api_cfg.get("password")),
        "hme_service_token": _secret_state(hme_cfg.service_token),
        "cf_mail_admin_password": _secret_state(mail_password),
    }
    return {
        "connections": {
            "codex_base_url": codex_url,
            "codex": {"configured": bool(codex_url and codex_key)},
            "sub2api_base_url": sub2api_cfg.get("base_url") or DEFAULT_SUB2API_BASE_URL,
            "sub2api_admin_email": sub2api_cfg.get("email") or "",
            "hme_base_url": hme_cfg.base_url or DEFAULT_HME_BASE_URL,
            "hme_account_id": hme_cfg.account_id or "",
            "cf_mail_base_url": (await get_setting_value(db, CF_SETTING_BASE_URL, DEFAULT_CF_MAIL_BASE_URL) or DEFAULT_CF_MAIL_BASE_URL),
            "cf_mail_address": (await get_setting_value(db, CF_SETTING_ADDRESS, DEFAULT_CF_MAIL_ADDRESS) or DEFAULT_CF_MAIL_ADDRESS),
            "sub2api": {
                "configured": bool(sub2api_cfg.get("configured")),
                "auth_mode": "api_key" if sub2api_cfg.get("api_key") else ("admin" if sub2api_cfg.get("email") and sub2api_cfg.get("password") else "none"),
            },
            "hme": {"configured": bool(hme_cfg.configured)},
            "mail": {"configured": mail_configured},
        },
        "sub2api_push": push_defaults.model_dump(),
        "codex_rs": await load_codex_rs_settings(db),
        "automation": {
            "official_quota_probe": stored_quota,
            "quota_runtime": quota_runtime,
            "auto_reauth": reauth_cfg,
            "auto_rotate": rotate_cfg,
            "rotation_workspaces": rotation_workspaces,
            "invite_seat_wire": invite_seat_wire,
        },
        "resources": {
            "sms_max_uses_per_phone": phones_cfg.max_uses,
            "sms_cooldown_sec": phones_cfg.cooldown_sec,
            "sms_reserve_sec": phones_cfg.reserve_sec,
        },
        "account": {"username": env.admin_username},
        "secret_state": secret_state,
        "secrets": {
            "codex_admin_key": "",
            "sub2api_api_key": _secret_view(sub2api_cfg.get("api_key")),
            "sub2api_admin_password": _secret_view(sub2api_cfg.get("password")),
            "hme_token": _secret_view(hme_cfg.service_token),
            "hme_service_token": _secret_view(hme_cfg.service_token),
            "cf_mail_admin_password": _secret_view(mail_password),
        },
    }


async def save_console_settings(db: AsyncSession, payload) -> dict[str, Any]:
    if payload.connections is not None:
        conn = payload.connections
        if conn.codex_base_url is not None or conn.codex_admin_key is not None:
            from app.integrations.codex.client import normalize_url
            from app.application.tokens import encrypt_secret
            from app.persistence.models.codex import CodexBinding
            old_url = await get_setting_value(db, "codex_base_url", "") or ""
            new_url = old_url if conn.codex_base_url is None else (normalize_url(conn.codex_base_url) if conn.codex_base_url.strip() else "")
            if new_url and new_url != old_url:
                if _keep_secret(conn.codex_admin_key):
                    raise ValueError("更改 Codex 地址时必须重新输入目标管理员 API Key")
                foreign_binding = await db.scalar(select(CodexBinding.account_id).where(CodexBinding.target_url != new_url).limit(1))
                if foreign_binding is not None:
                    raise ValueError("已有账号绑定其他 Codex 地址，请先核对绑定，禁止直接更换目标")
            if not _keep_secret(conn.codex_admin_key):
                key = conn.codex_admin_key.strip()
                if any(ord(c) < 33 or ord(c) > 126 for c in key):
                    raise ValueError("Codex API Key 格式无效")
                await upsert_setting(db, "codex_admin_key_encrypted", encrypt_secret(key))
            await upsert_setting(db, "codex_base_url", new_url)
        mapping = {
            "sub2api_base_url": conn.sub2api_base_url,
            "sub2api_admin_email": conn.sub2api_admin_email,
            "hme_base_url": conn.hme_base_url,
            "hme_account_id": conn.hme_account_id,
            "cf_mail_base_url": conn.cf_mail_base_url,
            "cf_mail_address": conn.cf_mail_address,
        }
        for key, value in mapping.items():
            if value is not None:
                await upsert_setting(db, key, str(value).strip())
        secrets = {
            "sub2api_api_key": conn.sub2api_api_key,
            "sub2api_admin_password": conn.sub2api_admin_password,
            "hme_service_token": conn.hme_service_token,
            "cf_mail_admin_password": conn.cf_mail_admin_password,
        }
        for key, value in secrets.items():
            if value is None or _keep_secret(value):
                continue
            await upsert_setting(db, key, str(value).strip())
    if payload.automation is not None and payload.automation.official_quota_probe is not None:
        await upsert_setting(
            db,
            "official_quota_probe_enabled",
            "true" if payload.automation.official_quota_probe else "false",
        )
    if payload.automation is not None and payload.automation.auto_reauth is not None:
        await upsert_setting(
            db,
            "auto_reauth_enabled",
            "true" if payload.automation.auto_reauth else "false",
        )
    if payload.automation is not None:
        from app.application.rotate import rotate_service
        from app.persistence.models.identity import Workspace
        automation = payload.automation
        current = await rotate_service.load_settings(db)
        scope = automation.auto_rotate_scope or current["auto_rotate_scope"]
        ids = (sorted(set(automation.auto_rotate_workspace_ids)) if automation.auto_rotate_workspace_ids is not None
               else current["auto_rotate_workspace_ids"])
        enabled = automation.auto_rotate if automation.auto_rotate is not None else current["auto_rotate_enabled"]
        if automation.auto_rotate_workspace_ids is not None and ids:
            existing = set(await db.scalars(select(Workspace.id).where(Workspace.id.in_(ids))))
            if existing != set(ids):
                raise ValueError("所选工作空间已不存在，请刷新后重新选择")
        if enabled and scope == "selected" and not ids:
            raise ValueError("请先选择至少一个工作空间，或明确选择全部工作空间")
        if automation.auto_rotate_scope is not None:
            await upsert_setting(db, "auto_rotate_scope", scope)
        if automation.auto_rotate_workspace_ids is not None:
            await upsert_setting(db, "auto_rotate_workspace_ids", json.dumps(ids))
        if payload.automation.auto_rotate is not None:
            await upsert_setting(db, "auto_rotate_enabled", "true" if payload.automation.auto_rotate else "false")
        if payload.automation.auto_rotate_daily_limit is not None:
            await upsert_setting(db, "auto_rotate_daily_limit", str(payload.automation.auto_rotate_daily_limit))
    if payload.sub2api_push is not None:
        from app.application.sub2api_defaults import save_defaults

        await save_defaults(db, payload.sub2api_push)
    if payload.codex_rs is not None:
        await save_codex_rs_settings(db, payload.codex_rs)
    if payload.resources is not None:
        res = payload.resources
        numbers = {
            "sms_max_uses_per_phone": res.sms_max_uses_per_phone,
            "sms_cooldown_sec": res.sms_cooldown_sec,
            "sms_reserve_sec": res.sms_reserve_sec,
        }
        for key, value in numbers.items():
            if value is not None:
                await upsert_setting(db, key, str(int(value)))
    if payload.password is not None:
        old_password = payload.password.old_password
        new_password = payload.password.new_password
        confirm_password = payload.password.confirm_password
        if new_password != confirm_password:
            raise ValueError("两次输入的新密码不一样")
        changed = await change_admin_password(db, old_password, new_password)
        if not changed:
            raise ValueError("当前密码不对")
    await db.commit()
    return await load_console_settings(db)
