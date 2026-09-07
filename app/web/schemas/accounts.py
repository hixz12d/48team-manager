"""Local identity registration, without credentials or remote membership writes."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator
from app.domain.identity.ids import normalize_email


class DeleteAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    confirmation_email: str = Field(min_length=3, max_length=255)


class RegisterAccountRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(min_length=3, max_length=255)
    purpose: Literal["standby", "child"] = "standby"

    @field_validator("email")
    @classmethod
    def valid_email(cls, value):
        value = normalize_email(value)
        if value.count("@") != 1 or any(c.isspace() for c in value):
            raise ValueError("邮箱格式不正确")
        local, domain = value.split("@")
        if not local or not domain or "." not in domain or any(c in value for c in '<>;,"'):
            raise ValueError("邮箱格式不正确")
        return value
