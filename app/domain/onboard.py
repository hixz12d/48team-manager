"""Onboard / refill policy. Browser last; vacancy still gates auto refill."""

from __future__ import annotations

import secrets
import string

from app.domain.rotate import KICK_COOLDOWN_SECONDS
MEMBER_LOOKUP_RETRIES = 3
MEMBER_LOOKUP_INTERVAL = 3.0
JOIN_CONFIRM_ATTEMPTS = 8
JOIN_CONFIRM_INTERVAL = 5.0


def random_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(12)) + "Aa1!"


def classify_onboard_error(error: str, *, stage: str = "") -> str:
    text = (error or "").lower()
    message = error or ""
    if "cancelled" in text or "已取消" in message:
        return "cancelled"
    if (
        "account_deactivated" in text
        or "has been deactivated" in text
        or "is deactivated" in text
        or "deactivated_workspace" in text
        or "已被 deactivate" in message
    ):
        return "account_deactivated"
    if "coroutine" in text:
        return "browser_await_bug"
    if "too many" in text or "限流" in message or "max_check" in text:
        return "openai_rate_limited"
    if "仍未通过" in message or "mail_otp_rejected" in text:
        return "mail_otp_rejected"
    if "sms_rejected" in text or "不被 openai 接受" in text or "停在美国" in message or "绑满" in message:
        return "sms_rejected"
    if "号码池" in message or "phone_pool" in text:
        return "phone_pool_empty"
    if "sms_missing" in text or "sms_failed" in text or "接码" in message or "手机号页" in message or "sms" in text:
        return "sms_failed"
    if "otp" in text or "mailbox" in text or "验证码" in message:
        return "mail_otp_timeout"
    if "邮箱页" in message or "email_gate" in text or "email_input_missing" in text:
        return "email_gate_stuck"
    if "about_you" in text or "年龄/生日" in message or "填写年龄" in message:
        return "about_you_stuck"
    if "cloudflare" in text or "just a moment" in text or "turnstile" in text:
        return "cloudflare_challenge"
    if "代理" in message or "proxy" in text:
        return "proxy_failed"
    if "已满" in message or "full" in text:
        return "team_full"
    if "冷却" in message or "cooldown" in text:
        return "kick_cooldown"
    if stage == "reconcile" or "对账" in message or "未看到该成员" in message:
        return "not_joined"
    if stage == "push" or "sub2api" in text:
        return "push_failed"
    if stage == "invite":
        return "invite_failed"
    if "refresh_token" in text or "没有 refresh" in message:
        return "oauth_no_refresh"
    if "登录的是" in message:
        return "oauth_identity_mismatch"
    if stage == "oauth" or "无法自动授权" in message or "自动授权" in message:
        return "oauth_failed"
    return "browser_failed"
