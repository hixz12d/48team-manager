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
    return SECRET_MASK


def _keep_secret(value: str | None) -> bool:
    text = str(value or "").strip()
    return (not text) or text == SECRET_MASK


async def load_console_settings(db: AsyncSession) -> dict[str, Any]:
    from app.application.resources.hme import DEFAULT_HME_BASE_URL, load_config as load_hme_config
    from app.application.resources.phones import phone_pool_service
    from app.domain.rotate import DEFAULT_AUTO_ROTATE_DAILY_LIMIT
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
    phones_cfg = await phone_pool_service.get_config(db)
    stored_quota = as_bool(
        await get_setting_value(db, "official_quota_probe_enabled", str(bool(env.official_quota_probe_enabled)).lower()),
        bool(env.official_quota_probe_enabled),
    )
    stored_reauth = as_bool(
        await get_setting_value(db, "auto_reauth_enabled", str(bool(env.auto_reauth_enabled)).lower()),
        bool(env.auto_reauth_enabled),
    )
    stored_rotate = as_bool(
        await get_setting_value(db, "auto_rotate_enabled", str(bool(env.auto_rotate_enabled)).lower()),
        bool(env.auto_rotate_enabled),
    )
    stored_force = as_bool(
        await get_setting_value(db, "auto_rotate_force_refill", str(bool(env.force_refill)).lower()),
        bool(env.force_refill),
    )
    daily_raw = await get_setting_value(db, "auto_rotate_daily_limit", str(DEFAULT_AUTO_ROTATE_DAILY_LIMIT))
    try:
        daily_limit = max(0, int(daily_raw or DEFAULT_AUTO_ROTATE_DAILY_LIMIT))
    except (TypeError, ValueError):
        daily_limit = DEFAULT_AUTO_ROTATE_DAILY_LIMIT
    return {
        "connections": {
            "sub2api_base_url": sub2api_cfg.get("base_url") or DEFAULT_SUB2API_BASE_URL,
            "sub2api_admin_email": sub2api_cfg.get("email") or "",
            "hme_base_url": hme_cfg.base_url or DEFAULT_HME_BASE_URL,
            "hme_account_id": hme_cfg.account_id or "",
            "cf_mail_base_url": (await get_setting_value(db, CF_SETTING_BASE_URL, DEFAULT_CF_MAIL_BASE_URL) or DEFAULT_CF_MAIL_BASE_URL),
            "cf_mail_address": (await get_setting_value(db, CF_SETTING_ADDRESS, DEFAULT_CF_MAIL_ADDRESS) or DEFAULT_CF_MAIL_ADDRESS),
            "sub2api": {"configured": bool(sub2api_cfg.get("configured"))},
            "hme": {"configured": bool(hme_cfg.configured)},
        },
        "automation": {
            "official_quota_probe": stored_quota,
            "auto_reauth": stored_reauth,
            "auto_rotate": stored_rotate,
            "force_refill": stored_force,
            "auto_rotate_daily_limit": daily_limit,
            "env": {
                "auto_reauth": bool(env.auto_reauth_enabled),
                "auto_rotate": bool(env.auto_rotate_enabled),
                "force_refill": bool(env.force_refill),
            },
        },
        "resources": {
            "sms_max_uses_per_phone": phones_cfg.max_uses,
            "sms_cooldown_sec": phones_cfg.cooldown_sec,
            "sms_reserve_sec": phones_cfg.reserve_sec,
        },
        "account": {"username": env.admin_username},
        "secrets": {
            "sub2api_api_key": _secret_view(sub2api_cfg.get("api_key")),
            "sub2api_admin_password": _secret_view(sub2api_cfg.get("password")),
            "hme_token": _secret_view(hme_cfg.service_token),
            "hme_service_token": _secret_view(hme_cfg.service_token),
            "cf_mail_admin_password": _secret_view(await get_setting_value(db, CF_SETTING_ADMIN_PASSWORD, "")),
        },
    }


async def save_console_settings(db: AsyncSession, payload) -> dict[str, Any]:
    if payload.connections is not None:
        conn = payload.connections
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
    if payload.automation is not None:
        auto = payload.automation
        flags = {
            "official_quota_probe_enabled": auto.official_quota_probe,
            "auto_reauth_enabled": auto.auto_reauth,
            "auto_rotate_enabled": auto.auto_rotate,
            "auto_rotate_force_refill": auto.force_refill,
        }
        for key, value in flags.items():
            if value is not None:
                await upsert_setting(db, key, "true" if value else "false")
        if auto.auto_rotate_daily_limit is not None:
            await upsert_setting(db, "auto_rotate_daily_limit", str(int(auto.auto_rotate_daily_limit)))
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
