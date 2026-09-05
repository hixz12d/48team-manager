"""Workspace write payloads. Secrets stay write-only."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
