"""Resource write payloads for phones and proxies."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PhoneImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)


class ProxyCreateRequest(BaseModel):
    url: str = Field(min_length=3, max_length=500)
    name: str | None = Field(default=None, max_length=120)


class ProxyPatchRequest(BaseModel):
    name: str | None = Field(default=None, max_length=120)
    restore_auto_name: bool = False
    status: Literal["active", "disabled"] | None = None


class OnboardRequest(BaseModel):
    email_line: str = Field(default="", max_length=500)
    phone_line: str = Field(default="", max_length=500)
    proxy: str = Field(default="", max_length=500)
    password: str = Field(default="", max_length=200)
    force: bool = False
    skip_invite: bool = False


class RotateRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    email_line: str = Field(default="", max_length=500)
    phone_line: str = Field(default="", max_length=500)
    proxy: str = Field(default="", max_length=500)
    force_refill: bool = False
    reason: str = Field(default="console", max_length=120)


class KickRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    user_id: str | None = Field(default=None, max_length=120)
    reason: str = Field(default="console_kick", max_length=120)
    unbind_sub2api: bool = False


class RevokeInviteRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class AccountProxyPatch(BaseModel):
    proxy: str | None = Field(default=None, max_length=500)
    proxy_profile_id: int | None = None
    clear: bool = False


class PhoneStatusPatch(BaseModel):
    status: Literal["active", "disabled"]


class Sub2ApiPushRequest(BaseModel):
    group_ids: list[int] = Field(default_factory=list)
    name: str | None = Field(default=None, max_length=255)
    schedulable: bool | None = True
    confirm_mixed_channel_risk: bool = False


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


class WorkspaceRemoveChildRequest(BaseModel):
    email: str = Field(default="", max_length=320)
    account_id: int | None = None


class WorkspacePurgeChildRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    user_id: str | None = Field(default=None, max_length=120)
    reason: str = Field(default="console_purge", max_length=120)
