"""子号池重新授权：iCloud 自动，母号/Gmail 弹窗。"""
from __future__ import annotations

from typing import Any, Dict


ICLOUD_DOMAINS = frozenset({"icloud.com", "me.com", "mac.com"})


def is_icloud_email(email: str) -> bool:
    text = (email or "").strip().lower()
    if "@" not in text:
        return False
    return text.rsplit("@", 1)[1] in ICLOUD_DOMAINS


def is_oauth_callback(url: str) -> bool:
    text = (url or "").strip().lower()
    if "localhost:1455/" not in text and "127.0.0.1:1455/" not in text:
        return False
    return "code=" in text or "/auth/callback" in text


def owner_refresh_allows_oauth(error_code: str) -> bool:
    """RT/ST 换票失败才改走弹窗；封号/串号不要打开授权页。"""
    return str(error_code or "") in {"", "token_refresh_failed"}


def auto_reauth_plan(
    *,
    email: str,
    role: str,
    password: str = "",
    pickup_url: str = "",
    cf_ready: bool = False,
    proxy: str = "",
) -> Dict[str, Any]:
    if (role or "") == "owner":
        return {"auto": False, "reason": "母号请用弹出窗口自己走 Gmail 登录"}
    if not is_icloud_email(email):
        return {"auto": False, "reason": "非 iCloud 子号，请用弹出窗口自己登录"}
    if not (password or "").strip():
        return {"auto": False, "reason": "这个 iCloud 子号没存密码，改走手动授权"}
    if not (pickup_url or "").strip() and not cf_ready:
        return {"auto": False, "reason": "没有邮箱读码配置，改走手动授权"}
    if not (proxy or "").strip():
        return {"auto": False, "reason": "没有静态 ISP 代理，自动授权跑不了，改走手动弹窗"}
    return {
        "auto": True,
        "reason": "iCloud 子号将自动登录、读验证码、接码并写回 Sub2API",
    }
