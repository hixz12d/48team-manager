"""Decode ChatGPT access tokens without verifying signatures."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import jwt

from app.core.time import utcnow


class JWTParser:
    def decode_token(self, token: str) -> dict[str, Any] | None:
        try:
            return jwt.decode(
                token,
                options={"verify_signature": False, "verify_exp": False},
            )
        except Exception:
            return None

    def extract_email(self, token: str) -> str | None:
        payload = self.decode_token(token)
        if not payload:
            return None
        profile = payload.get("https://api.openai.com/profile") or {}
        email = profile.get("email") or payload.get("email")
        return str(email).strip().lower() if email else None

    def extract_client_id(self, token: str) -> str | None:
        payload = self.decode_token(token)
        if not payload:
            return None
        value = payload.get("client_id")
        return str(value) if value else None

    def extract_user_id(self, token: str) -> str | None:
        payload = self.decode_token(token)
        if not payload:
            return None
        auth = payload.get("https://api.openai.com/auth") or {}
        value = auth.get("user_id")
        return str(value) if value else None

    def expiration_utc(self, token: str) -> datetime | None:
        payload = self.decode_token(token)
        if not payload:
            return None
        exp = payload.get("exp")
        if not exp:
            return None
        try:
            return datetime.fromtimestamp(int(exp), tz=timezone.utc)
        except (TypeError, ValueError, OSError):
            return None

    def remaining_seconds(self, token: str, *, now: datetime | None = None) -> float | None:
        exp = self.expiration_utc(token)
        if exp is None:
            return None
        stamp = now or utcnow()
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return (exp - stamp).total_seconds()


jwt_parser = JWTParser()
