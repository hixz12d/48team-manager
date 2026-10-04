"""Standby pool payloads."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PoolImportRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class PoolJoinRequest(BaseModel):
    workspace_id: int
    role: Literal["owner", "member"] = "member"
    seat_intent: Literal["workspace_default", "standard", "premium"] = "workspace_default"
    # 被替换的子号邮箱；为空表示直接拉入（团队要有空位）
    replace_email: str = Field(default="", max_length=255)
