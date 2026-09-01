"""Identity invariants. Email, plan, and names are never source of truth."""

from __future__ import annotations

from app.domain.identity import LOCAL_PURPOSES, OFFICIAL_PLANS, WORKSPACE_ROLES


def normalize_official_plan(value: str | None) -> str:
    plan = (value or "unknown").strip().lower()
    return plan if plan in OFFICIAL_PLANS else "unknown"


def normalize_workspace_role(value: str | None) -> str:
    role = (value or "unknown").strip().lower()
    return role if role in WORKSPACE_ROLES else "unknown"


def normalize_local_purpose(value: str | None) -> str:
    purpose = (value or "").strip().lower()
    if purpose not in LOCAL_PURPOSES:
        raise ValueError("local purpose must be assigned explicitly")
    return purpose


def purpose_from_plan(plan: str) -> None:
    """Official plan never implies local purpose."""
    normalize_official_plan(plan)
    return None


def role_from_email(email: str) -> None:
    """Gmail / iCloud / family names never imply workspace role."""
    _ = email
    return None
