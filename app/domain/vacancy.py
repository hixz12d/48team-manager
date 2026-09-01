"""Kick receipts are evidence only. Auto refill never guesses from missing notices."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.core.time import utcnow, zone
from app.domain.identity.ids import normalize_email

HISTORY_LIMIT = 30


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text.lstrip("-").isdigit():
            return int(text)
    return None


def _as_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    return None


def _as_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _json_text(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except TypeError:
        return str(value)


def to_aware_utc(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        dt = value
    else:
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


def to_local_naive(value: Any, *, tz_name: str = "Asia/Shanghai") -> datetime | None:
    dt = to_aware_utc(value)
    if dt is None:
        return None
    return dt.astimezone(zone(tz_name)).replace(tzinfo=None)


def parse_policy_notice(payload: Any) -> dict[str, Any] | None:
    return parse_removal_notices(payload)


def parse_removal_notices(payload: Any) -> dict[str, Any] | None:
    """Extract kick receipts. Missing both notice keys returns None."""
    if not isinstance(payload, dict):
        return None
    has_policy_key = "policy_notice" in payload
    has_billing_key = "billing_notice" in payload
    if not has_policy_key and not has_billing_key:
        return None

    policy = payload.get("policy_notice") if has_policy_key else None
    billing = payload.get("billing_notice") if has_billing_key else None
    policy_null = has_policy_key and policy is None
    policy_obj = policy if isinstance(policy, dict) else None

    ordinal = _as_int(policy_obj.get("vacancy_ordinal")) if policy_obj else None
    threshold = _as_int(policy_obj.get("free_vacancy_threshold")) if policy_obj else None
    is_free = None
    if ordinal is not None and threshold is not None:
        is_free = ordinal < threshold

    return {
        "policy_notice_null": policy_null,
        "has_policy_notice": policy_obj is not None,
        "has_billing_notice": billing is not None,
        "policy_kind": _as_text(policy_obj.get("kind")) if policy_obj else None,
        "billed_seat_delta": _as_int(policy_obj.get("billed_seat_delta")) if policy_obj else None,
        "replacement_required": _as_bool(policy_obj.get("replacement_required")) if policy_obj else None,
        "vacancy_ordinal": ordinal,
        "free_vacancy_threshold": threshold,
        "billing_starts_at": to_aware_utc(policy_obj.get("billing_starts_at")) if policy_obj else None,
        "expires_at": to_aware_utc(policy_obj.get("expires_at")) if policy_obj else None,
        "is_free": is_free,
        "policy_notice_json": _json_text(policy) if has_policy_key else None,
        "billing_notice_json": _json_text(billing) if has_billing_key else None,
    }


def chatgpt_member_ids(*candidates: Any) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if not isinstance(value, str):
            return
        text = value.strip()
        if not text or text in seen:
            return
        seen.add(text)
        values.append(text)

    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("id", "member_id", "account_user_id", "user_id"):
                add(candidate.get(key))
        else:
            add(candidate)
    preferred = [item for item in values if item.startswith("user-")]
    rest = [item for item in values if not item.startswith("user-")]
    return preferred + rest


def pick_chatgpt_user_id(*candidates: Any) -> str | None:
    ids = chatgpt_member_ids(*candidates)
    return ids[0] if ids else None


def _format_metric(value: Any, *, null_policy: bool) -> str:
    if null_policy and value is None:
        return "null"
    if value is None:
        return "--"
    return str(value)


def _format_time(value: Any, *, null_policy: bool) -> str:
    if null_policy and value is None:
        return "null"
    if value is None:
        return "--"
    if isinstance(value, datetime):
        local = to_local_naive(value) or value
        return local.strftime("%Y-%m-%d %H:%M:%S")
    local = to_local_naive(value)
    return local.strftime("%Y-%m-%d %H:%M:%S") if local else "--"


def is_safe_to_refill(vacancy: dict[str, Any] | None) -> bool:
    """Only refill when ordinal is below threshold and billing/replacement are clear."""
    if not vacancy:
        return False
    if vacancy.get("has_billing_notice"):
        return False
    if vacancy.get("replacement_required") is True:
        return False
    return vacancy.get("is_free") is True


def present_vacancy(vacancy: dict[str, Any] | None) -> dict[str, Any] | None:
    if not vacancy:
        return None

    null_policy = bool(vacancy.get("policy_notice_null"))
    ordinal = vacancy.get("vacancy_ordinal")
    threshold = vacancy.get("free_vacancy_threshold")
    is_free = vacancy.get("is_free")
    has_billing = bool(vacancy.get("has_billing_notice"))
    replacement_required = vacancy.get("replacement_required")
    safe = is_safe_to_refill(
        {
            **vacancy,
            "is_free": is_free,
            "has_billing_notice": has_billing,
            "replacement_required": replacement_required,
        }
    )

    if null_policy:
        comparison = "policy_notice: null"
        status_label = "无阈值"
    elif ordinal is not None and threshold is not None:
        comparison = f"{ordinal} {'<' if ordinal < threshold else '≥'} {threshold}"
        status_label = "可释放" if is_free else "未释放"
    elif vacancy.get("has_policy_notice"):
        comparison = "--"
        status_label = "未知"
    else:
        comparison = "无 policy_notice"
        status_label = "未知"

    if has_billing:
        status_label = "有账单回执"
    elif replacement_required is True:
        status_label = "需替换席位"

    if safe:
        tone = "ok"
    elif is_free is False or has_billing or replacement_required is True:
        tone = "warn"
    else:
        tone = "muted"

    captured_at = vacancy.get("captured_at")
    billing_starts = vacancy.get("billing_starts_at")
    expires = vacancy.get("expires_at")
    return {
        "id": vacancy.get("id"),
        "workspace_id": vacancy.get("workspace_id") or vacancy.get("team_id"),
        "team_id": vacancy.get("workspace_id") or vacancy.get("team_id"),
        "account_id": vacancy.get("account_id") or "",
        "user_id": vacancy.get("user_id") or "",
        "email": vacancy.get("email") or "",
        "policy_notice_null": null_policy,
        "has_policy_notice": bool(vacancy.get("has_policy_notice")),
        "has_billing_notice": has_billing,
        "policy_kind": vacancy.get("policy_kind") or "",
        "billed_seat_delta": vacancy.get("billed_seat_delta"),
        "replacement_required": replacement_required,
        "vacancy_ordinal": ordinal,
        "free_vacancy_threshold": threshold,
        "vacancy_ordinal_text": _format_metric(ordinal, null_policy=null_policy),
        "free_vacancy_threshold_text": _format_metric(threshold, null_policy=null_policy),
        "billing_starts_at": billing_starts.isoformat() if isinstance(billing_starts, datetime) else billing_starts,
        "expires_at": expires.isoformat() if isinstance(expires, datetime) else expires,
        "billing_starts_at_text": _format_time(billing_starts, null_policy=null_policy),
        "expires_at_text": _format_time(expires, null_policy=null_policy),
        "is_free": is_free,
        "safe_to_refill": safe,
        "refill_label": "可自动补位" if safe else "勿自动补位",
        "billing_notice_text": "有" if has_billing else "无",
        "comparison_text": comparison,
        "status_label": status_label,
        "tone": tone,
        "captured_at": captured_at.isoformat() if isinstance(captured_at, datetime) else captured_at,
        "captured_at_text": _format_time(captured_at, null_policy=False) if captured_at else "--",
    }


def summarize_for_message(vacancy: dict[str, Any] | None) -> str:
    presented = present_vacancy(vacancy) if vacancy and "status_label" not in vacancy else vacancy
    if not presented:
        return ""
    parts = [presented.get("status_label") or "", presented.get("comparison_text") or ""]
    if presented.get("refill_label"):
        parts.append(presented["refill_label"])
    if presented.get("is_free") is False and presented.get("expires_at_text") not in {None, "", "--", "null"}:
        parts.append(f"释放 {presented['expires_at_text']}")
    return "，".join(part for part in parts if part and part != "--")


def vacancy_email(email: str | None) -> str | None:
    text = normalize_email(email)
    return text or None


def captured_now(now: datetime | None = None) -> datetime:
    return now or utcnow()
