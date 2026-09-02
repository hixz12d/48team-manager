"""Resource write payloads for phones and proxies."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PhoneImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)


class ProxyCreateRequest(BaseModel):
    url: str = Field(min_length=3, max_length=500)
    name: str | None = Field(default=None, max_length=120)
