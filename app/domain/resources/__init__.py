"""Phones, HME aliases, and proxy profiles."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

STATUS_ACTIVE = "active"
STATUS_MAXED = "maxed"
STATUS_DISABLED = "disabled"
STATUS_RISK = "risk"

OUTCOME_SUCCESS = "success"
OUTCOME_INVALID = "invalid"
OUTCOME_RECENTLY_USED = "recently_used"
OUTCOME_RISK = "risk"
OUTCOME_NO_SMS = "no_sms"
OUTCOME_CANCELLED = "cancelled"
OUTCOME_PROVIDER_ERROR = "provider_error"

DEFAULT_MAX_USES = 3
DEFAULT_COOLDOWN_SEC = 1200
DEFAULT_RESERVE_SEC = 180

HME_STATE_RESERVED = "reserved"
HME_STATE_SIGNUP_STARTED = "signup_started"
HME_STATE_CONSUMED = "consumed"
HME_STATE_QUARANTINED = "quarantined"
HME_STATE_MANUAL_REVIEW = "manual_review"
HME_HELD_STATES = (
    HME_STATE_SIGNUP_STARTED,
    HME_STATE_CONSUMED,
    HME_STATE_QUARANTINED,
    HME_STATE_MANUAL_REVIEW,
)
FREE_ACCOUNT_LABEL = "GPT已使用"
LEASE_TTL = timedelta(minutes=25)
SIGNUP_STARTED_STAGES = {"email_otp", "about_you", "add_phone", "oauth"}
SIGNUP_STARTED_ERROR_CODES = {
    "mail_otp_timeout",
    "mail_otp_rejected",
    "about_you_stuck",
    "sms_failed",
    "sms_rejected",
    "sms_missing",
    "oauth_callback_missing",
    "oauth_expired",
}
PRE_SIGNUP_ERROR_CODES = {
    "browser_failed",
    "proxy_failed",
    "proxy_missing",
    "mail_missing",
    "cancelled",
    "hme_error",
    "hme_unconfigured",
    "hme_empty",
    "hme_busy",
    "hme_no_account",
    "hme_account_ambiguous",
    "already_on_team",
    "oauth_url_missing",
    "email_input_missing",
    "email_gate_stuck",
}

_E164_RE = re.compile(r"^\+[1-9]\d{7,14}$")
_SERIAL_DIGIT_RE = re.compile(r"^\d+$")
_SERIAL_ALIAS_RE = re.compile(r"^别名\s*\d+$", re.I)


def normalize_phone_number(value: str) -> str:
    raw = str(value or "").strip()
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise ValueError("phone number is empty")
    if len(digits) == 10:
        digits = "1" + digits
    number = "+" + digits
    if not _E164_RE.fullmatch(number):
        raise ValueError(f"invalid phone number: {raw}")
    return number


def parse_phone_line(text: str) -> tuple[str, str]:
    raw = str(text or "").strip()
    if "----" not in raw:
        return raw, ""
    number, url = raw.split("----", 1)
    return number.strip(), url.strip()


def is_serial_label(label: str) -> bool:
    tag = str(label or "").strip()
    return bool(_SERIAL_DIGIT_RE.fullmatch(tag) or _SERIAL_ALIAS_RE.fullmatch(tag))


def is_unoccupied_label(label: str) -> bool:
    tag = str(label or "").strip()
    return (not tag) or is_serial_label(tag)


def should_occupy_failed_claim(result: dict[str, Any] | None = None) -> bool:
    payload = result if isinstance(result, dict) else {}
    if payload.get("success"):
        return False
    if payload.get("occupy_alias") is True:
        return True
    if payload.get("occupy_alias") is False:
        return False
    code = str(payload.get("error_code") or "").strip().lower()
    if code in PRE_SIGNUP_ERROR_CODES:
        return False
    stage = str(payload.get("stage") or payload.get("status") or "").strip().lower()
    if stage in {"queued", "checking", "hme", "browser_open", "mail_missing"}:
        return False
    return bool(code or stage)


def signup_started_from_progress(*, stage: str = "", error_code: str = "") -> bool:
    code = str(error_code or "").strip().lower()
    if code in SIGNUP_STARTED_ERROR_CODES:
        return True
    return str(stage or "").strip().lower() in SIGNUP_STARTED_STAGES


def parse_created_at(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def locale_key_zh_cn(text: str) -> tuple[str, str]:
    value = str(text or "")
    return (value.casefold(), value)


def normalize_hme_base_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("HME url is empty")
    if "://" not in text:
        text = "http://" + text
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("HME url is invalid")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("HME url must be http or https")
    if parsed.query or parsed.fragment:
        raise ValueError("HME url must not include query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("HME url must not include a path")
    return f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")
