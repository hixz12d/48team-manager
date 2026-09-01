"""Workspace write payloads. Secrets stay write-only."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RegisterWorkspaceRequest(BaseModel):
    email: str = Field(min_length=3, max_length=255)
    official_workspace_id: str = Field(min_length=8, max_length=100)
    name: str | None = Field(default=None, max_length=255)
    seat_limit: int | None = Field(default=None, ge=1, le=100)
    proxy: str | None = Field(default=None, max_length=500)
    password: str | None = Field(default=None, max_length=200)
    access_token: str | None = None
    refresh_token: str | None = None
    session_token: str | None = None
    id_token: str | None = None
    client_id: str | None = Field(default=None, max_length=100)
