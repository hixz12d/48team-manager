"""Workspace write payloads. Secrets stay write-only."""

from __future__ import annotations

from typing import Literal
from datetime import date
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ProxySelection(BaseModel):
    source: Literal["sub2api"]
    remote_id: int = Field(gt=0)


class StartWorkspaceOAuthRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    proxy: str | None = Field(default=None, max_length=500)
    proxy_selection: ProxySelection | None = None

    @model_validator(mode="after")
    def validate_proxy_choice(self):
        if self.proxy and self.proxy_selection is not None:
            raise ValueError("proxy 与 proxy_selection 不能同时提交")
        return self


class CompleteWorkspaceOAuthRequest(BaseModel):
    ticket: str = Field(min_length=8, max_length=200)
    callback_url: str = Field(min_length=8, max_length=4000)


class CompleteAccountOAuthRequest(BaseModel):
    ticket: str = Field(min_length=8, max_length=200)
    callback_url: str = Field(min_length=8, max_length=4000)
    # Optional follow-ups after a successful exchange; omitted means the old behaviour.
    workspace_id: int | None = Field(default=None, gt=0)
    push_sub2api: bool = False
    count_switch: bool = False


PHONE_SESSION_PATTERN = r"^[a-f0-9]{32}$"


class ExtensionHandoffRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    workspace_id: int = Field(gt=0)
    sync_operation_id: str | None = Field(default=None, min_length=4, max_length=80)
    # Phone relay session of this extension job (docs/contracts/phone-relay.md §7).
    phone_session: str | None = Field(default=None, pattern=PHONE_SESSION_PATTERN)


class ExtensionPhoneRequest(BaseModel):
    """Local-extension SMS relay body (docs/contracts/phone-relay.md)."""
    model_config = ConfigDict(extra="ignore")

    action: Literal["acquire", "code", "report", "release"]
    phase: Literal["signup", "oauth"] = "signup"
    phoneId: int | None = Field(default=None, ge=0)
    bound: bool = False
    outcome: Literal["success", "invalid", "recently_used", "risk", "no_sms", "wrong_code", "cancelled"] | None = None
    session: str = Field(pattern=PHONE_SESSION_PATTERN)
    email: str = Field(min_length=3, max_length=254)
    workspaceId: int | None = Field(default=None, gt=0)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value):
        return value.strip().lower() if isinstance(value, str) else value

    @model_validator(mode="after")
    def required_fields(self):
        if self.action in {"code", "report"} and self.phoneId is None:
            raise ValueError("phoneId is required")
        if self.action == "report" and self.outcome is None:
            raise ValueError("outcome is required")
        return self


class ExtensionResolveRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)


class ExtensionHandoffCompleteRequest(BaseModel):
    account_id: int = Field(gt=0)
    workspace_id: int = Field(gt=0)
    ticket: str = Field(min_length=8, max_length=200)
    callback_url: str = Field(min_length=8, max_length=4000)
    push_sub2api: bool = True
    count_switch: bool = True


class WorkspaceExpiryPatch(BaseModel):
    # Required key: an omitted field must never silently clear a saved reminder.
    expires_on: date | None

    @field_validator("expires_on", mode="before")
    @classmethod
    def validate_calendar_date(cls, value):
        if value is None:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value, flags=re.ASCII):
            raise ValueError("请选择有效日期，格式为 YYYY-MM-DD；清空请传 null")
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("请选择有效的日历日期") from exc
