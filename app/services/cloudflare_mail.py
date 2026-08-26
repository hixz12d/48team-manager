"""Cloudflare Temp Email 读码。接口对齐 icloud-hme 的 GET /admin/mails。"""
from __future__ import annotations

import email
import logging
from email.header import decode_header, make_header
from email.utils import parseaddr
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.services.mail_otp import extract_code, extract_invite_url

logger = logging.getLogger(__name__)

DEFAULT_CF_MAIL_BASE_URL = "https://apimail.xiaozhudf2026.foo"
DEFAULT_CF_MAIL_ADDRESS = "icloud@xiaozhudf2026.foo"
CF_SETTING_BASE_URL = "cf_mail_base_url"
CF_SETTING_ADDRESS = "cf_mail_address"
CF_SETTING_ADMIN_PASSWORD = "cf_mail_admin_password"


def normalize_cloudflare_base_url(raw: str) -> str:
    text = str(raw or "").strip()
    if not text:
        raise ValueError("Cloudflare API 地址不能为空")
    if "://" not in text:
        text = "https://" + text
    parsed = urlparse(text)
    if not parsed.scheme or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Cloudflare API 地址格式无效")
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("Cloudflare API 地址只支持 http 或 https")
    if parsed.query or parsed.fragment:
        raise ValueError("Cloudflare API 地址不能包含查询参数或片段")
    if parsed.path not in {"", "/"}:
        raise ValueError("Cloudflare API 地址不能包含路径")
    return f"{parsed.scheme}://{parsed.hostname}" + (f":{parsed.port}" if parsed.port else "")


def normalize_mailbox_address(raw: str) -> str:
    address = str(raw or "").strip().lower()
    if not address or "@" not in address or " " in address:
        raise ValueError("Cloudflare 收件地址格式无效")
    local, domain = address.split("@", 1)
    if not local or not domain or "." not in domain:
        raise ValueError("Cloudflare 收件地址格式无效")
    return address


def _decode_header(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(make_header(decode_header(text)))
    except Exception:  # noqa: BLE001
        return text


def _cloudflare_hme_alias(header: str) -> str:
    for parameter in str(header or "").split(";"):
        name, sep, value = parameter.strip().partition("=")
        if not sep or name.strip().lower() != "p":
            continue
        candidate = value.strip().strip("\"<>")
        _, parsed = parseaddr(candidate)
        if parsed and "@" in parsed:
            return parsed.lower()
    return ""


def _looks_like_rfc822(raw: str) -> bool:
    if "\n" not in raw or ":" not in raw:
        return False
    lower = raw.lower()
    return "from:" in lower or "subject:" in lower or "x-icloud-hme:" in lower


def _raw_field(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        if key not in item:
            continue
        value = item.get(key)
        if value is None:
            continue
        if isinstance(value, (int, float)):
            return str(value)
        text = str(value).strip()
        if text and text.lower() != "null":
            return text
    return ""


def _unwrap_items(payload: Any, depth: int = 0) -> list[dict[str, Any]]:
    if depth > 3 or payload is None:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if not isinstance(payload, dict):
        return []
    for key in ("data", "results", "mails", "items", "value"):
        if key in payload:
            nested = _unwrap_items(payload[key], depth + 1)
            if nested:
                return nested
    return []




def parse_cloudflare_message(item: dict[str, Any]) -> dict[str, str]:
    raw = ""
    for key in ("raw", "message"):
        candidate = _raw_field(item, key)
        if _looks_like_rfc822(candidate):
            raw = candidate
            break

    subject = ""
    preview = ""
    to_addr = ""
    match_text = raw
    if raw:
        parsed = email.message_from_string(raw)
        subject = _decode_header(parsed.get("Subject", ""))
        to_addr = _cloudflare_hme_alias(parsed.get("X-ICLOUD-HME", "")) or _decode_header(parsed.get("To", ""))
        parts: list[str] = []
        if parsed.is_multipart():
            for part in parsed.walk():
                if part.get_content_maintype() == "multipart":
                    continue
                payload = part.get_payload(decode=True)
                charset = part.get_content_charset() or "utf-8"
                if payload:
                    try:
                        parts.append(payload.decode(charset, errors="replace"))
                    except Exception:  # noqa: BLE001
                        parts.append(payload.decode("utf-8", errors="replace"))
        else:
            payload = parsed.get_payload(decode=True)
            charset = parsed.get_content_charset() or "utf-8"
            if payload:
                try:
                    parts.append(payload.decode(charset, errors="replace"))
                except Exception:  # noqa: BLE001
                    parts.append(payload.decode("utf-8", errors="replace"))
        preview = "\n".join(parts).strip()
        match_text = "\n".join(
            [
                raw,
                " ".join(f"{key}: {value}" for key, value in parsed.items()),
                preview,
            ]
        )

    if not subject:
        subject = _decode_header(_raw_field(item, "subject"))
    if not to_addr:
        to_addr = _decode_header(_raw_field(item, "to", "address", "recipient", "rcpt"))
    if not preview:
        preview = _raw_field(item, "bodyPreview", "preview", "text", "content", "html", "body")
    match_text = "\n".join([match_text, to_addr, subject, preview])
    return {
        "subject": subject,
        "preview": preview,
        "to": to_addr,
        "match": match_text,
    }


def message_matches(message: dict[str, str], alias: str) -> bool:
    needle = str(alias or "").strip().lower()
    if not needle:
        return True
    blob = "\n".join(
        [
            message.get("from", ""),
            message.get("to", ""),
            message.get("subject", ""),
            message.get("preview", ""),
            message.get("match", ""),
        ]
    ).lower()
    return needle in blob


class CloudflareMailClient:
    def __init__(self, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def _get_json(self, url: str, *, admin_password: str, params: Optional[dict[str, Any]] = None) -> Any:
        with httpx.Client(timeout=self.timeout, follow_redirects=False) as client:
            response = client.get(
                url,
                params=params,
                headers={
                    "Accept": "application/json",
                    "x-admin-auth": admin_password,
                },
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"Cloudflare 邮件服务返回 HTTP {response.status_code}")
        try:
            return response.json()
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError("Cloudflare 邮件响应格式无效") from exc

    def fetch_messages(
        self,
        *,
        base_url: str,
        address: str,
        admin_password: str,
        alias: str = "",
        limit: int = 20,
    ) -> list[dict[str, str]]:
        origin = normalize_cloudflare_base_url(base_url)
        mailbox = normalize_mailbox_address(address)
        secret = str(admin_password or "").strip()
        if not secret:
            raise ValueError("Cloudflare 管理员密钥不能为空")
        payload = self._get_json(
            f"{origin}/admin/mails",
            admin_password=secret,
            params={"limit": max(1, min(int(limit), 50)), "offset": 0, "address": mailbox},
        )
        messages = [parse_cloudflare_message(item) for item in _unwrap_items(payload)]
        if alias:
            messages = [item for item in messages if message_matches(item, alias)]
        return messages

    def find_code(self, *, base_url: str, address: str, admin_password: str, alias: str) -> Optional[str]:
        for message in self.fetch_messages(
            base_url=base_url,
            address=address,
            admin_password=admin_password,
            alias=alias,
        ):
            code = extract_code("\n".join([message.get("subject", ""), message.get("preview", "")])) or extract_code(message.get("match", ""))
            if code:
                return code
        return None

    def find_invite(self, *, base_url: str, address: str, admin_password: str, alias: str) -> Optional[str]:
        for message in self.fetch_messages(
            base_url=base_url,
            address=address,
            admin_password=admin_password,
            alias=alias,
        ):
            url = extract_invite_url("\n".join([message.get("preview", ""), message.get("match", "")]))
            if url:
                return url
        return None


cloudflare_mail_client = CloudflareMailClient()
