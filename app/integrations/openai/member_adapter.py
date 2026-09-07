"""Single official member/invite adapter. Nested and top-level fields stay equivalent."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain.identity.ids import normalize_email

EMAIL_KEYS = ("email", "email_address", "emailAddress", "invite_email")
USER_ID_KEYS = ("id", "user_id", "userId", "account_user_id", "member_id")
NAME_KEYS = ("name", "display_name", "displayName", "full_name")
ROLE_KEYS = ("role", "official_role", "role_name", "workspace_role")
SEAT_TYPE_KEYS = ("seat_type", "seatType", "seat")
TIME_KEYS = ("added_at", "joined_at", "created_at", "invited_at", "addedAt", "joinedAt", "createdAt")
NESTED_OBJECT_KEYS = ("user", "account", "profile", "invite", "member", "invited_user")
COLLECTION_KEYS = ("items", "users", "members", "invites", "results", "data")
TOTAL_KEYS = ("total", "count", "reported_total", "total_count", "totalCount")
SEAT_LIMIT_KEYS = ("seat_limit", "seats_limit", "max_seats", "seatLimit", "capacity", "seats")
OCCUPIED_SEAT_KEYS = ("occupied_seats", "used_seats", "seats_used", "occupiedSeats", "usedSeats", "seat_used")
OWNER_ROLES = {"owner", "account-owner", "account_owner", "workspace-owner", "workspace_owner", "admin-owner"}
ADMIN_ROLES = {"admin", "account-admin", "account_admin", "workspace-admin", "workspace_admin"}
MEMBER_ROLES = {"member", "standard-user", "standard_user", "user", "account-member", "account_member"}
DOMAIN_ROLE_OWNER = "owner"
DOMAIN_ROLE_ADMIN = "admin"
DOMAIN_ROLE_MEMBER = "member"
DOMAIN_ROLE_UNKNOWN = "unknown"
INVITE_DOMAIN_ROLES = (DOMAIN_ROLE_OWNER, DOMAIN_ROLE_MEMBER)
INVITE_ROLE_PAYLOAD = {
    DOMAIN_ROLE_OWNER: "account-owner",
    DOMAIN_ROLE_MEMBER: "standard-user",
}
INVITE_SEAT_TYPE_PREMIUM = "premium"
INVITE_SEAT_TYPE_STANDARD = "default"
DEFAULT_INVITE_SEAT_TYPE = INVITE_SEAT_TYPE_PREMIUM
JOINED_STATES = {"joined", "active", "member", "accepted"}
INVITED_STATES = {"invited", "pending", "invite", "invitation"}


def _as_text(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list, bool)):
        return None
    text = str(value).strip()
    return text or None


def _dig_text(item: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        text = _as_text(item.get(key))
        if text:
            return text
    return None


def parse_official_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def unwrap_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    if isinstance(data, dict):
        merged = dict(payload)
        for key, value in data.items():
            merged.setdefault(key, value)
        return merged
    return payload


def extract_item_list(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    body = unwrap_payload(payload)
    if not body:
        return []
    for key in COLLECTION_KEYS:
        value = body.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            nested = extract_item_list(value)
            if nested:
                return nested
    return []


def extract_reported_total(payload: Any) -> int | None:
    body = unwrap_payload(payload)
    if not body:
        return None
    for key in TOTAL_KEYS:
        value = body.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            return number
    pagination = body.get("pagination") if isinstance(body.get("pagination"), dict) else None
    if pagination:
        return extract_reported_total(pagination)
    return None


def extract_seat_metadata(payload: Any) -> dict[str, Any]:
    body = unwrap_payload(payload)
    meta: dict[str, Any] = {}
    if not body:
        return meta
    for key in SEAT_LIMIT_KEYS:
        value = body.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            meta["seat_limit"] = number
            break
    for key in OCCUPIED_SEAT_KEYS:
        value = body.get(key)
        if value is None or isinstance(value, bool):
            continue
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number >= 0:
            meta["occupied_seats"] = number
            break
    billing = body.get("billing") if isinstance(body.get("billing"), dict) else None
    if billing:
        nested = extract_seat_metadata(billing)
        for key, value in nested.items():
            meta.setdefault(key, value)
    account = body.get("account") if isinstance(body.get("account"), dict) else None
    if account:
        nested = extract_seat_metadata(account)
        for key, value in nested.items():
            meta.setdefault(key, value)
    return meta


def _nested_dicts(item: dict[str, Any]) -> list[dict[str, Any]]:
    found = [item]
    for key in NESTED_OBJECT_KEYS:
        value = item.get(key)
        if isinstance(value, dict):
            found.append(value)
    return found


def _email_from(item: dict[str, Any]) -> str:
    for node in _nested_dicts(item):
        email = normalize_email(_dig_text(node, EMAIL_KEYS) or "")
        if email:
            return email
    return ""


def _user_id_from(item: dict[str, Any]) -> str | None:
    for node in _nested_dicts(item):
        for key in USER_ID_KEYS:
            value = _as_text(node.get(key))
            if value and (value.startswith("user-") or key != "id" or "user" in key.lower()):
                if value.startswith("user-") or key in {"user_id", "userId", "account_user_id", "member_id"}:
                    return value
        user = node.get("user")
        if isinstance(user, dict):
            value = _as_text(user.get("id") or user.get("user_id"))
            if value:
                return value
    for node in _nested_dicts(item):
        value = _as_text(node.get("id"))
        if value and value.startswith("user-"):
            return value
    return None


def _name_from(item: dict[str, Any]) -> str | None:
    for node in _nested_dicts(item):
        name = _dig_text(node, NAME_KEYS)
        if name:
            return name
    return None


def _role_from(item: dict[str, Any]) -> str:
    for node in _nested_dicts(item):
        role = _dig_text(node, ROLE_KEYS)
        if role:
            return role
    return "unknown"


def _seat_type_from(item: dict[str, Any]) -> str | None:
    for node in _nested_dicts(item):
        seat = _dig_text(node, SEAT_TYPE_KEYS)
        if seat:
            return seat
    return None


def _added_at_from(item: dict[str, Any]) -> datetime | None:
    for node in _nested_dicts(item):
        for key in TIME_KEYS:
            parsed = parse_official_datetime(node.get(key))
            if parsed is not None:
                return parsed
    return None


def _state_from(item: dict[str, Any], default_state: str) -> str:
    raw = ""
    for node in _nested_dicts(item):
        raw = str(node.get("state") or node.get("status") or node.get("membership_state") or "").strip().lower()
        if raw:
            break
    if raw in JOINED_STATES:
        return "joined"
    if raw in INVITED_STATES:
        return "invited"
    return "invited" if default_state == "invited" else "joined"


def is_owner_role(role: str | None) -> bool:
    return normalize_official_role(role) == DOMAIN_ROLE_OWNER


def is_admin_role(role: str | None) -> bool:
    return normalize_official_role(role) == DOMAIN_ROLE_ADMIN


def is_member_role(role: str | None) -> bool:
    return normalize_official_role(role) == DOMAIN_ROLE_MEMBER


def normalize_official_role(value: str | None) -> str:
    role = str(value or "").strip().lower().replace("_", "-")
    if not role:
        return DOMAIN_ROLE_UNKNOWN
    if role in OWNER_ROLES or role.replace("-", "_") in {item.replace("-", "_") for item in OWNER_ROLES}:
        return DOMAIN_ROLE_OWNER
    if role in ADMIN_ROLES or role.replace("-", "_") in {item.replace("-", "_") for item in ADMIN_ROLES}:
        return DOMAIN_ROLE_ADMIN
    if role in MEMBER_ROLES or role.replace("-", "_") in {item.replace("-", "_") for item in MEMBER_ROLES}:
        return DOMAIN_ROLE_MEMBER
    return DOMAIN_ROLE_UNKNOWN


def parse_invite_role(value: str | None, *, default: str = DOMAIN_ROLE_OWNER) -> str:
    role = normalize_official_role(value) if value not in (None, "") else default
    if role not in INVITE_DOMAIN_ROLES:
        raise ValueError("invite role must be owner or member")
    return role


def invite_role_payload(role: str | None, *, default: str = DOMAIN_ROLE_OWNER) -> str:
    return INVITE_ROLE_PAYLOAD[parse_invite_role(role, default=default)]


def parse_invite_seat_type(value: str | None, *, default: str = DEFAULT_INVITE_SEAT_TYPE) -> str:
    raw = str(value or default or "").strip().lower()
    if raw in {"premium", "premium-user", "premium_user"}:
        return INVITE_SEAT_TYPE_PREMIUM
    if raw in {"default", "standard", "standard-user", "member"}:
        return INVITE_SEAT_TYPE_STANDARD
    raise ValueError("invite seat type must be premium or standard")


def official_roles_equivalent(left: str | None, right: str | None) -> bool:
    a = normalize_official_role(left)
    b = normalize_official_role(right)
    if a == DOMAIN_ROLE_UNKNOWN or b == DOMAIN_ROLE_UNKNOWN:
        return False
    return a == b


def normalize_official_member(item: Any, *, default_state: str = "joined") -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    email = _email_from(item)
    if not email:
        return None
    raw_role = _role_from(item)
    role = normalize_official_role(raw_role) if raw_role and raw_role != "unknown" else DOMAIN_ROLE_UNKNOWN
    return {
        "email": email,
        "user_id": _user_id_from(item),
        "name": _name_from(item),
        "role": role,
        "raw_role": raw_role or DOMAIN_ROLE_UNKNOWN,
        "seat_type": _seat_type_from(item),
        "state": _state_from(item, default_state),
        "added_at": _added_at_from(item),
        "raw_keys": sorted(str(key) for key in item.keys()),
    }


def adapt_collection(items: Any, *, default_state: str = "joined") -> dict[str, Any]:
    raw_items = [item for item in (items or [])]
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    invalid = 0
    for item in raw_items:
        adapted = normalize_official_member(item, default_state=default_state)
        if adapted is None:
            invalid += 1
            continue
        email = adapted["email"]
        if email in seen:
            if adapted.get("state") == "joined":
                for index, existing in enumerate(parsed):
                    if existing["email"] == email:
                        parsed[index] = adapted
                        break
            continue
        seen.add(email)
        parsed.append(adapted)
    return {
        "raw_item_count": len(raw_items),
        "parsed_item_count": len(parsed),
        "invalid_item_count": invalid,
        "members": parsed,
    }


def validate_fetch_counts(
    *,
    reported_total: int | None,
    raw_item_count: int,
    parsed_item_count: int,
    invalid_item_count: int,
    incomplete: bool = False,
) -> dict[str, Any]:
    reported = int(reported_total) if reported_total is not None else None
    if incomplete:
        return {
            "ok": False,
            "error_code": "incomplete",
            "error": "official member fetch incomplete",
        }
    if (reported or 0) > 0 and parsed_item_count == 0:
        return {
            "ok": False,
            "error_code": "schema_mismatch",
            "error": "official response reported members but none could be parsed",
        }
    if raw_item_count > 0 and parsed_item_count == 0:
        return {
            "ok": False,
            "error_code": "schema_mismatch",
            "error": "official member list was non-empty but email fields were not recognized",
        }
    if reported is not None and raw_item_count > 0 and reported > 0:
        if raw_item_count >= reported and parsed_item_count == 0:
            return {
                "ok": False,
                "error_code": "schema_mismatch",
                "error": "official member schema mismatch",
            }
        if parsed_item_count == 0 and invalid_item_count == raw_item_count:
            return {
                "ok": False,
                "error_code": "schema_mismatch",
                "error": "official member items had no usable email",
            }
        if reported >= 2 and parsed_item_count > 0 and parsed_item_count < min(reported, raw_item_count) * 0.5:
            return {
                "ok": False,
                "error_code": "schema_mismatch",
                "error": "official member parsed count diverged from reported total",
            }
    return {"ok": True, "error_code": "", "error": ""}


def serialize_official_member(item: dict[str, Any]) -> dict[str, Any]:
    added_at = item.get("added_at")
    return {
        "email": item.get("email"),
        "user_id": item.get("user_id"),
        "name": item.get("name"),
        "role": item.get("role") or "unknown",
        "seat_type": item.get("seat_type"),
        "state": item.get("state") or "joined",
        "added_at": added_at.isoformat() if isinstance(added_at, datetime) else added_at,
        "is_owner": is_owner_role(item.get("role")),
    }
