"""SystemSetting helpers. Query payloads never include raw secrets."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth import change_admin_password
from app.core.config import load_settings
from app.persistence.models.settings import SystemSetting

TRUTHY = {"1", "true", "yes", "on"}
SECRET_MASK = "••••••"


async def get_setting_value(db: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == key))).scalar_one_or_none()
    if row is None or row.value is None:
        return default
    return row.value


async def upsert_setting(db: AsyncSession, key: str, value: str, description: str | None = None) -> SystemSetting:
    row = (await db.execute(select(SystemSetting).where(SystemSetting.key == key))).scalar_one_or_none()
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
        "automation": {
            "official_quota_probe": stored_quota,
            "quota_runtime": quota_runtime,
            "auto_reauth": reauth_cfg,
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
