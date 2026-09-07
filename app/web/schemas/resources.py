"""Resource write payloads for phones and proxies."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.web.schemas.workspaces import ProxySelection
from app.integrations.sms.client import parse_phone_line


class PhoneImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)




class OnboardRequest(BaseModel):
    email_line: str = Field(default="", max_length=500)
    phone_line: str = Field(default="", max_length=500)
    proxy: str = Field(default="", max_length=500)
    proxy_selection: ProxySelection | None = None
    password: str = Field(default="", max_length=200)
    force: bool = False
    skip_invite: bool = False
    role: Literal["owner", "member"] = "owner"

    @model_validator(mode="after")
    def validate_proxy_choice(self):
        if self.proxy and self.proxy_selection is not None:
            raise ValueError("proxy 与 proxy_selection 不能同时提交")
        return self


class ReplenishRequest(BaseModel):
    phone_line: str = Field(default="", max_length=500)
    role: Literal["owner", "member"] = "owner"

    @field_validator("phone_line")
    @classmethod
    def valid_phone_line(cls, value):
        raw = str(value or "").strip()
        if not raw:
            return ""
        number, sms_url = parse_phone_line(raw)
        if not number or not sms_url:
            raise ValueError("手机号格式应为 +1xxxxxxxxxx----https://...")
        return f"{number}----{sms_url}"


class RotateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    email_line: str = Field(default="", max_length=500)
    phone_line: str = Field(default="", max_length=500)
    proxy: str = Field(default="", max_length=500)
    force_refill: bool = False
    reason: str = Field(default="console", max_length=120)
    role: Literal["owner", "member"] = "owner"


class KickRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    user_id: str | None = Field(default=None, max_length=120)
    reason: str = Field(default="console_kick", max_length=120)
    unbind_sub2api: bool = False


class RevokeInviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class AccountProxyPatch(BaseModel):
    proxy: str | None = Field(default=None, max_length=500)
    proxy_selection: ProxySelection | None = None
    clear: bool = False

    @model_validator(mode="after")
    def validate_proxy_choice(self):
        if self.requested_modes() != 1:
            raise ValueError("必须且只能选择 proxy、proxy_selection 或 clear 之一")
        return self

    def requested_modes(self) -> int:
        return sum((bool(self.proxy), self.proxy_selection is not None, self.clear))


class AccountAutomationPatch(BaseModel):
    auto_reauth_opt_in: bool



class PhoneStatusPatch(BaseModel):
    status: Literal["active", "disabled"]


class Sub2ApiPushRequest(BaseModel):
    group_ids: list[int] | None = None
    name: str | None = Field(default=None, max_length=255)
    schedulable: bool | None = None
    confirm_mixed_channel_risk: bool = False


class Sub2ApiUsageSyncRequest(BaseModel):
    force_usage: bool = False




class OperationArchiveRequest(BaseModel):
    reason: str | None = Field(default=None, max_length=120)


class OperationBulkArchiveRequest(BaseModel):
    public_ids: list[str] = Field(default_factory=list)
    reason: str | None = Field(default=None, max_length=120)
    only_terminal: bool = True


class WorkspaceNamePatch(BaseModel):
    custom_name: str | None = Field(default=None, max_length=255)


class WorkspaceLinkMemberRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    account_id: int | None = None


class WorkspaceAddChildRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["owner", "member"] = "owner"


class WorkspaceRemoveChildRequest(BaseModel):
    email: str = Field(default="", max_length=320)
    account_id: int | None = None


class WorkspacePurgeChildRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    user_id: str | None = Field(default=None, max_length=120)
    reason: str = Field(default="console_purge", max_length=120)


class WorkspaceMemberRolePatch(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    role: Literal["owner", "member"] = "owner"
    user_id: str | None = Field(default=None, max_length=120)
