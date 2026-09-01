"""Settings payloads. Secrets stay write-only."""

from __future__ import annotations

from pydantic import BaseModel, Field


class ConnectionSettings(BaseModel):
    sub2api_base_url: str | None = None
    sub2api_api_key: str | None = None
    sub2api_admin_email: str | None = None
    sub2api_admin_password: str | None = None
    hme_base_url: str | None = None
    hme_service_token: str | None = None
    hme_account_id: str | None = None
    cf_mail_base_url: str | None = None
    cf_mail_address: str | None = None
    cf_mail_admin_password: str | None = None


class AutomationSettings(BaseModel):
    official_quota_probe: bool | None = None


class ConnectionProbeRequest(BaseModel):
    connections: ConnectionSettings | None = None


class ResourceSettings(BaseModel):
    sms_max_uses_per_phone: int | None = Field(default=None, ge=1, le=20)
    sms_cooldown_sec: int | None = Field(default=None, ge=60, le=86400)
    sms_reserve_sec: int | None = Field(default=None, ge=60, le=7200)


class PasswordSettings(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=6)
    confirm_password: str = Field(min_length=6)


class SettingsPatch(BaseModel):
    connections: ConnectionSettings | None = None
    automation: AutomationSettings | None = None
    resources: ResourceSettings | None = None
    password: PasswordSettings | None = None
