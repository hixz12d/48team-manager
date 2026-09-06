"""Official quota policy. Sub2API 429 never overrides a successful official 0%."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

from app.core.time import as_utc, utcnow

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
    request_count: int = 0
    http_status: int | None = None
    error_source: str = "official_quota"
    retry_after_at: datetime | None = None
    credential_revision: int | None = None
    check_id: str | None = None
    started_at: datetime | None = None

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
    except (TypeError, ValueError, OverflowError):
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
            try:
                return datetime.fromtimestamp(ts, tz=timezone.utc)
            except (ValueError, OverflowError, OSError):
                return None
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
    if all(clamp_percent((window or {}).get("used_percent")) is None for window in (five, seven)):
        return QuotaResult(success=False, error_code="parse_error", queried_at=queried_at)
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

    del slot_minute
    next_slot = now + timedelta(minutes=60)
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
    stamp = as_utc(now) or now
    for account in accounts:
        scheduled = as_utc(getattr(account, "next_quota_probe_at", None))
        if scheduled is None or scheduled <= stamp:
            due.append((scheduled or stamp, int(account.id)))
    due.sort()
    cap = max(0, int(limit))
    return [account_id for _, account_id in due[:cap]]


def quota_probe_user_message(error_code: Any = None, error_message: Any = None) -> str:
    code = str(error_code or "").strip()
    text = str(error_message or "").strip()
    nested_code = ""
    nested_message = ""
    if text.startswith("{") or text.startswith("["):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            err = payload.get("error")
            if isinstance(err, dict):
                nested_code = str(err.get("code") or "")
                nested_message = str(err.get("message") or "")
            elif isinstance(err, str):
                nested_message = err
            nested_code = nested_code or str(payload.get("code") or payload.get("error_code") or "")
            nested_message = nested_message or str(payload.get("message") or "")
    blob = " ".join(part for part in (code, nested_code, text, nested_message) if part).lower()
    if code == "missing_token" or "local access token" in blob or "undecryptable" in blob:
        return "这个号还没授权，无法读额度。点「授权」，用这个邮箱登录后再试。"
    if (
        code in {"token_revoked", "token_invalidated", "http_401"}
        or nested_code in {"token_revoked", "token_invalidated"}
        or "token_revoked" in blob
        or "invalidated oauth token" in blob
    ):
        return "官方登录已失效，点「授权」用这个邮箱重新登录后再读额度。"
    if code == "http_403":
        return "官方拒绝读取额度，请核对工作区和访问权限。"
    if code == "http_429":
        return "官方额度接口太频繁，稍后再试。"
    if code == "http_5xx":
        return "官方额度接口暂时不可用，稍后再试。"
    if text and not text.startswith("{") and not text.startswith("[") and len(text) <= 180:
        return text
    return "额度刷新失败，请重试。"


def official_overrides_sub2api_stale(official, sub2api_kind: str | None) -> bool:
    if official is None or not official.success or official.source != SOURCE_OFFICIAL:
        return False
    if official.seven_day_used_percent is None:
        return False
    return str(sub2api_kind or "") == "429" and int(official.seven_day_used_percent) < 100
