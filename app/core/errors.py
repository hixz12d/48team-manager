"""Typed errors for integrations and the console."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class IntegrationError(Exception):
    provider: str
    category: str
    message: str
    code: str | None = None
    retryable: bool = False
    technical_detail: str | None = None

    def __str__(self) -> str:
        return self.message

    def public_dict(self) -> dict:
        return {
            "provider": self.provider,
            "category": self.category,
            "retryable": self.retryable,
            "code": self.code,
            "message": self.message,
        }

    def technical_dict(self) -> dict:
        payload = self.public_dict()
        payload["technical_detail"] = self.technical_detail
        return payload
