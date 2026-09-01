"""Auto rotate policy. 5h and 401 never kick; weekly limit needs official 7d=100%."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.domain.quota import QuotaResult, SOURCE_OFFICIAL, clamp_percent

DEFAULT_AUTO_ROTATE_ENABLED = False
DEFAULT_AUTO_ROTATE_ON_DEACTIVATED = True
DEFAULT_AUTO_ROTATE_ON_WEEKLY_LIMIT = True
DEFAULT_AUTO_ROTATE_FORCE_REFILL = False
DEFAULT_AUTO_ROTATE_DAILY_LIMIT = 2
DEFAULT_AUTO_ROTATE_DRAIN_SECONDS = 2
KICK_COOLDOWN_SECONDS = 10 * 60
MAX_ROTATE_BACKOFF_HOURS = 6

UNBIND_SUB2API_REASONS = frozenset({"weekly_limit", "deactivated"})
WORKSPACE_LOCK_ACTIONS = ("rotate", "onboard", "reregister")


def classify_rotate_reason(
    *,
    kind: str,
    last_reauth_code: str = "",
    on_deactivated: bool = True,
    on_weekly_limit: bool = True,
) -> str | None:
    """Layer 3 candidates: deactivated or confirmed weekly limit. 5h and 401 stay out."""
    code = str(last_reauth_code or "")
    label = str(kind or "")
    if on_deactivated and code == "account_deactivated":
        return "deactivated"
    if label in {"401", "5h", "phone", "403"}:
        return None
    if on_weekly_limit and label == "429":
        return "weekly_limit"
    return None


def official_weekly_limit_full(usage: Any) -> bool | None:
    """True only when official 7d is 100%. None means unread; never treat as full."""
    if isinstance(usage, QuotaResult):
        if not usage.success or usage.source != SOURCE_OFFICIAL:
            return None
        if usage.seven_day_used_percent is None:
            return None
        return int(usage.seven_day_used_percent) >= 100
    payload = usage if isinstance(usage, dict) else {}
    seven = payload.get("seven_day") if isinstance(payload.get("seven_day"), dict) else {}
    util = seven.get("utilization")
    if util in (None, ""):
        extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
        util = extra.get("codex_7d_used_percent")
    if util in (None, ""):
        util = payload.get("seven_day_used_percent")
    percent = clamp_percent(util)
    if percent is None:
        return None
    return percent >= 100


def _parse_when(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        dt = value
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    if isinstance(value, (int, float)):
        ts = int(value)
        if ts > 10**12:
            ts //= 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)
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


def official_weekly_reset_at(usage: Any) -> datetime | None:
    if isinstance(usage, QuotaResult):
        return usage.seven_day_reset_at
    payload = usage if isinstance(usage, dict) else {}
    seven = payload.get("seven_day") if isinstance(payload.get("seven_day"), dict) else {}
    extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    return _parse_when(
        seven.get("resets_at")
        or extra.get("codex_7d_reset_at")
        or extra.get("codex_secondary_reset_at")
        or payload.get("seven_day_reset_at")
    )


def daily_auto_rotate_limit_reached(count: int, limit: int = DEFAULT_AUTO_ROTATE_DAILY_LIMIT) -> bool:
    return int(count or 0) >= max(0, int(limit))


def rotate_backoff_at(now: datetime, fail_count: int) -> datetime:
    attempts = max(1, int(fail_count))
    delay = 30 * 60 * (2 ** (attempts - 1))
    cap = MAX_ROTATE_BACKOFF_HOURS * 3600
    return now + timedelta(seconds=min(delay, cap))


def should_unbind_sub2api(reason: str) -> bool:
    return str(reason or "") in UNBIND_SUB2API_REASONS


def rotate_terminal_status(*, success: bool = False, error_code: str = "") -> str:
    if success:
        return "success"
    if str(error_code or "") in {
        "vacancy_not_safe_to_refill",
        "identity_conflict",
        "owner_manual",
        "resume_manual",
    }:
        return "manual_required"
    return "failed"
