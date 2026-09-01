"""Identity invariants. Email, plan, and names are never source of truth."""

from __future__ import annotations

from app.domain.identity import (
    AUTH_STATES,
    BINDING_STATES,
    LOCAL_PURPOSE_CHILD,
    LOCAL_PURPOSE_DISABLED,
    LOCAL_PURPOSE_FREE,
    LOCAL_PURPOSE_STANDBY,
    LOCAL_PURPOSES,
    MEMBERSHIP_STATE_INVITED,
    MEMBERSHIP_STATE_JOINED,
    MEMBERSHIP_STATE_REMOVED,
    MEMBERSHIP_STATE_UNKNOWN,
    MEMBERSHIP_STATES,
    OFFICIAL_PLANS,
    OPERATIONAL_STATES,
    WORKSPACE_ROLES,
    WORKSPACE_STATUSES,
)


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


def normalize_auth_state(value: str | None) -> str:
    state = (value or "unknown").strip().lower()
    return state if state in AUTH_STATES else "unknown"


def normalize_operational_state(value: str | None) -> str:
    state = (value or "available").strip().lower()
    return state if state in OPERATIONAL_STATES else "available"


def normalize_membership_state(value: str | None) -> str:
    state = (value or "unknown").strip().lower()
    return state if state in MEMBERSHIP_STATES else "unknown"


def normalize_binding_state(value: str | None) -> str:
    state = (value or "pending").strip().lower()
    return state if state in BINDING_STATES else "pending"


def normalize_workspace_status(value: str | None) -> str:
    status = (value or "active").strip().lower()
    return status if status in WORKSPACE_STATUSES else "unknown"


def purpose_from_plan(plan: str) -> None:
    """Official plan never implies local purpose."""
    normalize_official_plan(plan)
    return None


def role_from_email(email: str) -> None:
    """Gmail / iCloud / family names never imply workspace role."""
    _ = email
    return None


def child_local_purpose(status: str | None) -> str:
    if status == "free":
        return LOCAL_PURPOSE_FREE
    if status == "standby":
        return LOCAL_PURPOSE_STANDBY
    if status in {"disabled", "deleted"}:
        return LOCAL_PURPOSE_DISABLED
    return LOCAL_PURPOSE_CHILD


def child_operational_state(status: str | None) -> str:
    mapping = {
        "unused": "unused",
        "invited": "available",
        "active": "active",
        "standby": "standby",
        "disabled": "disabled",
        "deleted": "archived",
        "free": "free",
    }
    return mapping.get(status or "", "available")


def mapping_membership_state(status: str | None) -> str:
    if status in {MEMBERSHIP_STATE_INVITED, MEMBERSHIP_STATE_JOINED, MEMBERSHIP_STATE_REMOVED}:
        return status
    return MEMBERSHIP_STATE_UNKNOWN
