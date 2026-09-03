"""Workspace write payloads. Secrets stay write-only."""

from __future__ import annotations

from pydantic import BaseModel, Field


class StartWorkspaceOAuthRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    proxy: str | None = Field(default=None, max_length=500)


class CompleteWorkspaceOAuthRequest(BaseModel):
    ticket: str = Field(min_length=8, max_length=200)
    callback_url: str = Field(min_length=8, max_length=4000)


class CompleteAccountOAuthRequest(BaseModel):
    ticket: str = Field(min_length=8, max_length=200)
    callback_url: str = Field(min_length=8, max_length=4000)
