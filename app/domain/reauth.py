"""Reauth policy. 401 is not a ban; deactivated is manual_required."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from app.domain.automation import (
    DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES,
    MAX_AUTO_REAUTH_INTERVAL_MINUTES,
    MIN_AUTO_REAUTH_INTERVAL_MINUTES,
)

ICLOUD_DOMAINS = frozenset({"icloud.com", "me.com", "mac.com"})
DEACTIVATED_TOKENS = (
    "account has been deactivated",
    "this account is deactivated",
    "account is deactivated",
    "account_deactivated",
    "deactivated_workspace",
)
REAUTH_COOLDOWN_CODES = {
    "sms_rejected",
    "phone_pool_empty",
    "sms_failed",
    "mail_otp_timeout",
    "mail_otp_rejected",
}
MIN_REAUTH_BACKOFF_HOURS = 2
MAX_REAUTH_BACKOFF_HOURS = 6
HTTP_401_CODES = {"http_401", "token_invalidated", "unauthorized"}


def is_icloud_email(email: str) -> bool:
    text = (email or "").strip().lower()
    if "@" not in text:
        return False
    return text.rsplit("@", 1)[1] in ICLOUD_DOMAINS


def looks_like_deactivated(
    *,
    title: str = "",
    body: str = "",
    url: str = "",
    error: str = "",
) -> bool:
    blob = f"{title}\n{body}\n{url}\n{error}".lower()
    return any(token in blob for token in DEACTIVATED_TOKENS)


def is_oauth_callback(url: str) -> bool:
    text = (url or "").strip().lower()
    if "localhost:1455/" not in text and "127.0.0.1:1455/" not in text:
        return False
    return "code=" in text or "/auth/callback" in text


def owner_refresh_allows_oauth(error_code: str) -> bool:
    return str(error_code or "") != "token_identity_mismatch"


def reauth_terminal_status(*, success: bool = False, error_code: str = "") -> str:
    if success:
        return "success"
    if str(error_code or "") in {"account_deactivated", "identity_conflict", "owner_manual", "resume_manual"}:
        return "manual_required"
    return "failed"


def http_401_is_not_ban(error_code: str | None, status_code: int | None = None) -> bool:
    if str(error_code or "") in HTTP_401_CODES:
        return True
    try:
        return int(status_code or 0) == 401
    except (TypeError, ValueError):
        return False


def clamp_auto_reauth_interval_minutes(value: Any) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        number = DEFAULT_AUTO_REAUTH_INTERVAL_MINUTES
    return max(MIN_AUTO_REAUTH_INTERVAL_MINUTES, min(MAX_AUTO_REAUTH_INTERVAL_MINUTES, number))


def reauth_backoff_at(
    now: datetime,
    fail_count: int,
    *,
    error_code: str = "",
    min_hours: int = MIN_REAUTH_BACKOFF_HOURS,
    max_hours: int = MAX_REAUTH_BACKOFF_HOURS,
) -> datetime:
    attempts = max(1, int(fail_count))
    hours = int(min_hours) * (2 ** (attempts - 1))
    if str(error_code or "") in REAUTH_COOLDOWN_CODES:
        hours = max(hours, int(min_hours))
    return now + timedelta(hours=min(int(max_hours), hours))


def auto_reauth_plan(
    *,
    email: str,
    role: str,
    password: str = "",
    pickup_url: str = "",
    cf_ready: bool = False,
    proxy: str = "",
) -> dict[str, Any]:
    if (role or "") == "owner":
        return {"auto": False, "reason": "母号请用弹出窗口自己走 Gmail 登录"}
    if not is_icloud_email(email):
        return {"auto": False, "reason": "非 iCloud 子号，请用弹出窗口自己登录"}
    if not (pickup_url or "").strip() and not cf_ready:
        return {"auto": False, "reason": "没有邮箱读码配置，改走手动授权"}
    if not (proxy or "").strip():
        return {"auto": False, "reason": "没有静态 ISP 代理，自动授权跑不了，改走手动弹窗"}
    if not (password or "").strip():
        return {
            "auto": True,
            "reason": "iCloud 子号将走邮箱验证码登录、接码并写回本地凭证",
        }
    return {
        "auto": True,
        "reason": "iCloud 子号将自动登录、读验证码、接码并写回本地凭证",
    }
