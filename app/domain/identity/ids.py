"""Identity identifiers. Email and names never imply role or purpose."""

from __future__ import annotations

import re

from app.domain.identity import GMAIL_DOMAINS

WORKSPACE_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


def normalize_email(value: str | None) -> str:
    return str(value or "").strip().lower()


def looks_like_gmail(email: str | None) -> bool:
    normalized = normalize_email(email)
    if "@" not in normalized:
        return False
    return normalized.rsplit("@", 1)[1] in GMAIL_DOMAINS


def looks_like_user_id(value: str | None) -> bool:
    return str(value or "").strip().lower().startswith("user-")


def is_workspace_account_id(value: str | None) -> bool:
    text = str(value or "").strip().lower()
    if not text or looks_like_user_id(text):
        return False
    return bool(WORKSPACE_UUID_RE.fullmatch(text))


def workspace_official_id(raw: str | None) -> str | None:
    text = str(raw or "").strip()
    if not text:
        return None
    if looks_like_user_id(text) or not is_workspace_account_id(text):
        return None
    return text.lower()
