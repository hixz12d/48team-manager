"""Workspace write payloads. Secrets stay write-only."""

from __future__ import annotations

from typing import Literal
from datetime import date
import re

from pydantic import BaseModel, Field, field_validator, model_validator


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


class ExtensionHandoffRequest(BaseModel):
    email: str = Field(min_length=3, max_length=254)
    workspace_id: int = Field(gt=0)
    sync_operation_id: str | None = Field(default=None, min_length=4, max_length=80)


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
