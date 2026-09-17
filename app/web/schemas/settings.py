"""Settings payloads. Secrets stay write-only."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, PositiveInt, model_validator


class ConnectionSettings(BaseModel):
    codex_base_url: str | None = Field(default=None, max_length=500)
    codex_admin_key: str | None = Field(default=None, max_length=4096)
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
    auto_reauth: bool | None = None


class ConnectionProbeRequest(BaseModel):
    connections: ConnectionSettings | None = None
    target: Literal["sub2api", "hme", "mail", "all"] = "all"


class ResourceSettings(BaseModel):
    sms_max_uses_per_phone: int | None = Field(default=None, ge=1, le=20)
    sms_cooldown_sec: int | None = Field(default=None, ge=60, le=86400)
    sms_reserve_sec: int | None = Field(default=None, ge=60, le=7200)


class PasswordSettings(BaseModel):
    old_password: str = Field(min_length=1)
    new_password: str = Field(min_length=6)
    confirm_password: str = Field(min_length=6)


class Sub2ApiPushDefaults(BaseModel):
    concurrency: int = Field(default=5, ge=1, le=1000)
    group_ids: list[PositiveInt] = Field(default_factory=list, max_length=100)
    proxy_id: PositiveInt | None = None
    proxy_group_id: PositiveInt | None = None

    @model_validator(mode="after")
    def validate_selection(self):
        if self.proxy_id is not None and self.proxy_group_id is not None:
            raise ValueError("固定代理和代理分组只能选择一种")
        self.group_ids = list(dict.fromkeys(self.group_ids))
        return self


class SettingsPatch(BaseModel):
    sub2api_push: Sub2ApiPushDefaults | None = None
    connections: ConnectionSettings | None = None
    automation: AutomationSettings | None = None
    resources: ResourceSettings | None = None
    password: PasswordSettings | None = None
