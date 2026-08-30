"""OpenAI 官方额度客户端。

直打 chatgpt.com/backend-api/wham/usage。
只用本地 Token / Account ID / 代理，不经过 Sub2API。
解析失败或 HTTP 失败都返回 success=False，由调用方决定是否写快照；
本模块不改业务状态。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Protocol

import pytz

from app.config import settings

SOURCE_OFFICIAL = "official"
SOURCE_SUB2API = "sub2api"

FIVE_HOUR_SECONDS = 5 * 3600
SEVEN_DAY_SECONDS = 7 * 24 * 3600
FIVE_HOUR_MIN_SECONDS = 2 * 3600
FIVE_HOUR_MAX_SECONDS = 8 * 3600
SEVEN_DAY_MIN_SECONDS = 5 * 24 * 3600
SEVEN_DAY_MAX_SECONDS = 9 * 24 * 3600


class QuotaTransport(Protocol):
    async def fetch(
        self,
        access_token: str,
        db_session: Any,
        account_id: Optional[str],
        identifier: str,
    ) -> Dict[str, Any]:
        ...


@dataclass
class QuotaResult:
    success: bool
    source: str = SOURCE_OFFICIAL
    five_hour_used_percent: Optional[int] = None
    five_hour_reset_at: Optional[datetime] = None
    seven_day_used_percent: Optional[int] = None
    seven_day_reset_at: Optional[datetime] = None
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    queried_at: Optional[datetime] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    def can_drive_automation(self) -> bool:
        return (
            self.success
            and self.source == SOURCE_OFFICIAL
            and self.seven_day_used_percent is not None
        )


def classify_window_seconds(seconds: Any) -> Optional[str]:
    try:
        value = int(seconds)
    except (TypeError, ValueError):
        return None
    if FIVE_HOUR_MIN_SECONDS <= value <= FIVE_HOUR_MAX_SECONDS:
        return "five_hour"
    if SEVEN_DAY_MIN_SECONDS <= value <= SEVEN_DAY_MAX_SECONDS:
        return "seven_day"
    return None


def clamp_percent(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return max(0, min(100, int(round(float(value)))))
    except (TypeError, ValueError):
        return None


def to_naive_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    tz = pytz.timezone(settings.timezone)
    return value.astimezone(tz).replace(tzinfo=None)


def reset_at_from_window(window: Dict[str, Any], now: datetime) -> Optional[datetime]:
    raw = window.get("reset_at")
    if raw not in (None, ""):
        try:
            ts = int(raw)
        except (TypeError, ValueError):
            ts = None
        else:
            if ts > 10**12:
                ts //= 1000
            return to_naive_local(datetime.fromtimestamp(ts, tz=timezone.utc))
    after = window.get("reset_after_seconds")
    if after in (None, ""):
        return None
    try:
        return now + timedelta(seconds=int(after))
    except (TypeError, ValueError):
        return None


def _as_window(value: Any) -> Optional[Dict[str, Any]]:
    return value if isinstance(value, dict) else None


def parse_wham_usage(payload: Any, *, now: Optional[datetime] = None) -> QuotaResult:
    """把 /wham/usage JSON 映到 5h / 7d。窗口按 limit_window_seconds 分类，不写死 primary=5h。"""
    queried_at = now or datetime.utcnow()
    data = payload if isinstance(payload, dict) else {}
    rate_limit = data.get("rate_limit") if isinstance(data.get("rate_limit"), dict) else {}
    windows = [
        _as_window(rate_limit.get("primary_window")),
        _as_window(rate_limit.get("secondary_window")),
    ]
    extras = data.get("additional_rate_limits")
    if isinstance(extras, list):
        for item in extras:
            if not isinstance(item, dict):
                continue
            nested = item.get("rate_limit") if isinstance(item.get("rate_limit"), dict) else {}
            windows.extend(
                [
                    _as_window(nested.get("primary_window")),
                    _as_window(nested.get("secondary_window")),
                ]
            )

    five: Optional[Dict[str, Any]] = None
    seven: Optional[Dict[str, Any]] = None
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


class OpenAIQuotaClient:
    """官方额度查询。失败只返回结果，不写库、不改 schedulable、不踢人。"""

    def __init__(self, transport: Optional[QuotaTransport] = None):
        self._transport = transport

    async def fetch_quota(
        self,
        *,
        access_token: str,
        db_session: Any,
        workspace_id: Optional[str] = None,
        proxy: Optional[str] = None,
        identifier: str = "default",
        now: Optional[datetime] = None,
    ) -> QuotaResult:
        del proxy  # 代理走 ChatGPT 会话 / Account.proxy，不在这里另开通道。
        token = str(access_token or "").strip()
        if not token:
            return QuotaResult(
                success=False,
                error_code="missing_token",
                error_message="local access token missing",
                queried_at=now,
            )
        account_id = str(workspace_id or "").strip() or None
        transport = self._transport
        if transport is None:
            from app.services.chatgpt import chatgpt_service

            response = await chatgpt_service.get_wham_usage(
                token,
                db_session,
                account_id=account_id,
                identifier=identifier,
            )
        else:
            response = await transport.fetch(
                token,
                db_session,
                account_id,
                identifier,
            )
        if not isinstance(response, dict):
            return QuotaResult(
                success=False,
                error_code="transport",
                error_message="quota transport returned non-dict",
                queried_at=now,
            )
        if not response.get("success"):
            return QuotaResult(
                success=False,
                error_code=http_error_code(response.get("status_code"), response.get("error_code")),
                error_message=str(response.get("error") or "official quota request failed")[:500],
                queried_at=now,
                raw=response.get("data") if isinstance(response.get("data"), dict) else {},
            )
        return parse_wham_usage(response.get("data") or {}, now=now)


openai_quota_client = OpenAIQuotaClient()
