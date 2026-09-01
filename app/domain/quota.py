"""Official quota policy. Sub2API 429 never overrides a successful official 0%."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.core.time import utcnow

SOURCE_OFFICIAL = "official"
SOURCE_SUB2API = "sub2api"

FIVE_HOUR_MIN_SECONDS = 2 * 3600
FIVE_HOUR_MAX_SECONDS = 8 * 3600
SEVEN_DAY_MIN_SECONDS = 5 * 24 * 3600
SEVEN_DAY_MAX_SECONDS = 9 * 24 * 3600

SUCCESS_JITTER_SECONDS = 90
FAILURE_BACKOFF_MINUTES = (5, 15, 30, 60)
DEFAULT_QUOTA_PROBE_ENABLED = False
DEFAULT_QUOTA_PROBE_STAGGER_MINUTES = 60
DEFAULT_QUOTA_PROBE_BATCH_SIZE = 1
MIN_QUOTA_PROBE_BATCH_SIZE = 1
MAX_QUOTA_PROBE_BATCH_SIZE = 3
SKIP_OPERATIONAL_STATES = {"disabled", "archived"}


class QuotaTransport(Protocol):
    async def fetch(
        self,
        access_token: str,
        db_session: Any,
        account_id: str | None,
        identifier: str,
    ) -> dict[str, Any]:
        ...


@dataclass
class QuotaResult:
    success: bool
    source: str = SOURCE_OFFICIAL
    five_hour_used_percent: int | None = None
    five_hour_reset_at: datetime | None = None
    seven_day_used_percent: int | None = None
    seven_day_reset_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    queried_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def can_drive_automation(self) -> bool:
        return self.success and self.source == SOURCE_OFFICIAL and self.seven_day_used_percent is not None


def classify_window_seconds(seconds: Any) -> str | None:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return None
    if FIVE_HOUR_MIN_SECONDS <= value <= FIVE_HOUR_MAX_SECONDS:
        return "five_hour"
    if SEVEN_DAY_MIN_SECONDS <= value <= SEVEN_DAY_MAX_SECONDS:
        return "seven_day"
    return None


def clamp_percent(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def reset_at_from_window(window: dict[str, Any], now: datetime) -> datetime | None:
    raw = window.get("reset_at")
    if raw not in (None, ""):
        try:
            ts = int(raw)
        except (TypeError, ValueError):
            ts = None
        else:
            if ts > 10**12:
                ts //= 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc)
    after = window.get("reset_after_seconds")
    if after in (None, ""):
        return None
    try:
        return now + timedelta(seconds=int(after))
    except (TypeError, ValueError):
        return None


def parse_wham_usage(payload: Any, *, now: datetime | None = None) -> QuotaResult:
    queried_at = now or utcnow()
    data = payload if isinstance(payload, dict) else {}
    rate_limit = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}
    windows = [
        rate_limit.get("primary_window") if isinstance(rate_limit.get("primary_window"), dict) else None,
        rate_limit.get("secondary_window") if isinstance(rate_limit.get("secondary_window"), dict) else None,
    ]
    extras = data.get("additional_rate_limits")
    if isinstance(extras, list):
        for item in extras:
            if not isinstance(item, dict):
                continue
            nested = item.get("rate_limit") if isinstance(item.get("rate_limit"), dict) else {}
            windows.extend(
                [
                    nested.get("primary_window") if isinstance(nested.get("primary_window"), dict) else None,
                    nested.get("secondary_window") if isinstance(nested.get("secondary_window"), dict) else None,
                ]
            )
    five = None
    seven = None
    for window in windows:
        if not window:
            continue
        kind = classify_window_seconds(window.get("limit_window_seconds"))
        if kind == "five_hour" and five is None:
            five = window
        elif kind == "seven_day" and seven is None:
            seven = window
    if five is None and seven is None:
        return QuotaResult(
            success=False,
            source=SOURCE_OFFICIAL,
            error_code="parse_error",
            error_message="wham/usage missing 5h/7d windows",
            queried_at=queried_at,
            raw=data,
        )
    return QuotaResult(
        success=True,
        source=SOURCE_OFFICIAL,
        five_hour_used_percent=clamp_percent((five or {}).get("used_percent")),
        five_hour_reset_at=reset_at_from_window(five, queried_at) if five else None,
        seven_day_used_percent=clamp_percent((seven or {}).get("used_percent")),
        seven_day_reset_at=reset_at_from_window(seven, queried_at) if seven else None,
        queried_at=queried_at,
        raw=data,
    )


def http_error_code(status_code: Any, error_code: Any = None) -> str:
    if error_code:
        return str(error_code)
    try:
        status = int(status_code or 0)
    except (TypeError, ValueError):
        status = 0
    if status == 401:
        return "http_401"
    if status == 403:
        return "http_403"
    if status == 429:
        return "http_429"
    if status >= 500:
        return "http_5xx"
    if status > 0:
        return f"http_{status}"
    return "transport"


def quota_slot_minute_for(account_id: int) -> int:
    import hashlib

    digest = hashlib.sha1(f"official-quota:{int(account_id)}".encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 60


def initial_next_quota_probe_at(account_id: int, now: datetime, stagger_minutes: int) -> datetime:
    import hashlib

    window = max(1, int(stagger_minutes))
    digest = hashlib.sha1(f"official-quota-init:{int(account_id)}".encode("utf-8")).hexdigest()
    offset = int(digest[:8], 16) % (window * 60)
    return now + timedelta(seconds=offset)


def success_next_quota_probe_at(
    now: datetime,
    slot_minute: int,
    *,
    jitter_seconds: int = SUCCESS_JITTER_SECONDS,
    rng=None,
) -> datetime:
    import random

    slot = int(slot_minute) % 60
    next_slot = now.replace(minute=slot, second=0, microsecond=0)
    if next_slot <= now:
        next_slot += timedelta(hours=1)
    spread = max(0, int(jitter_seconds))
    dice = rng or random
    jitter = dice.randint(0, spread) if spread else 0
    return next_slot + timedelta(seconds=jitter)


def failure_next_quota_probe_at(now: datetime, fail_count: int) -> datetime:
    attempts = max(1, int(fail_count))
    minutes = FAILURE_BACKOFF_MINUTES[min(attempts, len(FAILURE_BACKOFF_MINUTES)) - 1]
    return now + timedelta(minutes=minutes)


def due_quota_account_ids(accounts, now: datetime, *, limit: int) -> list[int]:
    due: list[tuple[datetime, int]] = []
    for account in accounts:
        scheduled = account.next_quota_probe_at
        if scheduled is None or scheduled <= now:
            due.append((scheduled or now, int(account.id)))
    due.sort()
    cap = max(0, int(limit))
    return [account_id for _, account_id in due[:cap]]


def official_overrides_sub2api_stale(official, sub2api_kind: str | None) -> bool:
    if official is None or not official.success or official.source != SOURCE_OFFICIAL:
        return False
    if official.seven_day_used_percent is None:
        return False
    return str(sub2api_kind or "") == "429" and int(official.seven_day_used_percent) < 100
