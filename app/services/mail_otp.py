"""邮箱 OTP / 邀请链接轮询。支持 pickup URL 和 Cloudflare 自建邮箱。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

import httpx

from app.services.sms import require_proxy
from app.utils.proxy import build_httpx_proxy

logger = logging.getLogger(__name__)

CODE_RES = [
    re.compile(
        r"(?:verification code|one[-\s]?time(?:\s+password|\s+code)?|security code|login code|your code|enter code|"
        r"temporary (?:verification )?code|otp|验证码|校验码|临时验证码|一次性(?:验证)?码|安全码|动态密码|確認碼|确认码)"
        r"[^\d]{0,24}(\d{4,8})",
        re.I,
    ),
    re.compile(r"\b(\d{6})\b[^\w]{0,40}(?:is your|verification|one-time|security|验证码|是你的)", re.I),
    re.compile(r"(?:code|验证码)\s*[:=：]\s*(\d{6})\b", re.I),
]
_VERIFY_HINT = re.compile(
    r"verification code|one[-\s]?time(?:\s+password|\s+code)?|security code|login code|your code|enter code|"
    r"temporary (?:verification )?code|otp|验证码|校验码|临时验证码|一次性(?:验证)?码|安全码|动态密码|確認碼|确认码",
    re.I,
)
_ISOLATED_CODE_RE = re.compile(r"(?:-->|>)\s*(\d{6})\s*(?:<!--|<)")
_BARE_CODE_RE = re.compile(r"(?<![#\w])(\d{6})(?!\w)")
_STYLE_BLOCK_RE = re.compile(r"(?is)<style\b[^>]*>.*?</style>")
_SCRIPT_BLOCK_RE = re.compile(r"(?is)<script\b[^>]*>.*?</script>")
_HEX_COLOR_RE = re.compile(r"#[0-9A-Fa-f]{3,8}")
_YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_CHAT_HOST = r"(?:chatgpt|chat\.openai)\.com"
INVITE_RE = re.compile(rf"https?://{_CHAT_HOST}/[^\s\"'<>]+", re.I)
_INVITE_URL_RES = [
    re.compile(rf"https?://{_CHAT_HOST}/[^\s\"'<>]*(?:invite|accept-invite)[^\s\"'<>]*", re.I),
    re.compile(rf"https?://{_CHAT_HOST}/auth/login\?[^\s\"'<>]+", re.I),
]
_SKIP_INVITE_BITS = (
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".css", ".js", ".woff",
    "favicon", "/assets/", "/cdn", "/images/",
)
_BARE_CHAT_HOMES = {
    "https://chatgpt.com",
    "http://chatgpt.com",
    "https://chat.openai.com",
    "http://chat.openai.com",
}


def _usable_code(code: str) -> Optional[str]:
    value = str(code or "").strip()
    if len(value) == 6 and value.isdigit() and not _YEAR_RE.fullmatch(value) and value != "000000":
        return value
    return None


def _plain_mail_text(text: str) -> str:
    cleaned = _STYLE_BLOCK_RE.sub(" ", str(text or ""))
    cleaned = _SCRIPT_BLOCK_RE.sub(" ", cleaned)
    cleaned = _HEX_COLOR_RE.sub(" ", cleaned)
    cleaned = re.sub(r"(?is)<!--.*?-->", " ", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    return re.sub(r"\s+", " ", cleaned)


def extract_code(text: str) -> Optional[str]:
    blob = str(text or "")
    if not blob.strip():
        return None
    if extract_invite_url(blob) and not _VERIFY_HINT.search(blob):
        return None
    isolated = _ISOLATED_CODE_RE.search(blob)
    if isolated:
        code = _usable_code(isolated.group(1))
        if code:
            return code
    cleaned = _plain_mail_text(blob)
    if extract_invite_url(cleaned) and not _VERIFY_HINT.search(cleaned):
        return None
    for pattern in CODE_RES:
        match = pattern.search(cleaned)
        if match:
            code = _usable_code(match.group(1))
            if code:
                return code
    if _VERIFY_HINT.search(cleaned):
        match = _BARE_CODE_RE.search(cleaned)
        if match:
            return _usable_code(match.group(1))
    return None

def extract_invite_url(text: str) -> Optional[str]:
    blob = str(text or "")
    for pattern in _INVITE_URL_RES:
        match = pattern.search(blob)
        if match:
            return match.group(0).rstrip(").,;")
    for match in INVITE_RE.finditer(blob):
        url = match.group(0).rstrip(").,;")
        lower = url.lower()
        if lower.rstrip("/") in _BARE_CHAT_HOMES:
            continue
        if any(bit in lower for bit in _SKIP_INVITE_BITS):
            continue
        return url
    return None


def parse_mail_line(value: str) -> dict[str, str]:
    raw = str(value or "").strip()
    if not raw:
        return {"email": "", "password": "", "pickup_url": "", "refresh_token": "", "client_id": "", "use_cloudflare": False}

    parts = [part.strip() for part in re.split(r"----+|\|", raw) if part.strip()]
    email = parts[0] if parts else ""
    password = ""
    pickup_url = ""
    refresh_token = ""
    client_id = ""
    for part in parts[1:]:
        if part.startswith("http://") or part.startswith("https://"):
            pickup_url = part
        elif re.fullmatch(r"[0-9a-fA-F-]{32,}", part) or part.startswith("M."):
            refresh_token = part
        elif re.fullmatch(r"[0-9a-fA-F-]{8,}", part) and not client_id:
            client_id = part
        elif not password:
            password = part
    return {
        "email": email.split()[0] if email else "",
        "password": password,
        "pickup_url": pickup_url,
        "refresh_token": refresh_token,
        "client_id": client_id,
        "raw": raw,
        "use_cloudflare": not pickup_url and "@" in email,
    }


class MailOtpClient:
    def __init__(self, *, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def _iter_payloads(self, payload: Any) -> list[str]:
        blobs: list[str] = []
        if payload is None:
            return blobs
        if isinstance(payload, str):
            return [payload]
        if isinstance(payload, dict):
            for key in ("html", "text", "content", "body", "message", "mail", "data", "items", "messages"):
                if key in payload:
                    blobs.extend(self._iter_payloads(payload[key]))
            blobs.append(json.dumps(payload, ensure_ascii=False))
            return blobs
        if isinstance(payload, list):
            for item in payload:
                blobs.extend(self._iter_payloads(item))
        return blobs

    def fetch_mailbox(self, pickup_url: str, *, proxy: str, email: str = "") -> list[str]:
        normalized_proxy = require_proxy(proxy, "邮箱 OTP")
        parsed = urlparse(pickup_url)
        fragment = parse_qs(parsed.fragment)
        query = parse_qs(parsed.query)
        token = ""
        for source in (fragment, query):
            token = str((source.get("key") or source.get("token") or [""])[0]).strip()
            if token:
                break
        headers = {"Accept": "application/json, text/html"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if email:
            headers["X-Mailbox-Email"] = email

        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            proxy=build_httpx_proxy(normalized_proxy),
        ) as client:
            response = client.get(pickup_url, headers=headers)
            text = response.text
        blobs = [text]
        try:
            blobs.extend(self._iter_payloads(response.json()))
        except Exception:  # noqa: BLE001
            pass
        return blobs

    def find_code(self, pickup_url: str, *, proxy: str, email: str = "") -> Optional[str]:
        for blob in self.fetch_mailbox(pickup_url, proxy=proxy, email=email):
            code = extract_code(blob)
            if code:
                return code
        return None

    def find_invite(self, pickup_url: str, *, proxy: str, email: str = "") -> Optional[str]:
        for blob in self.fetch_mailbox(pickup_url, proxy=proxy, email=email):
            url = extract_invite_url(blob)
            if url:
                return url
        return None

    def wait_for_code(
        self,
        pickup_url: str,
        *,
        proxy: str,
        email: str = "",
        timeout_sec: float = 120,
        poll_interval_sec: float = 2.0,
    ) -> str:
        deadline = time.time() + timeout_sec
        last_error: Optional[Exception] = None
        while time.time() < deadline:
            try:
                code = self.find_code(pickup_url, proxy=proxy, email=email)
                if code:
                    return code
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("邮箱 OTP 轮询失败: %s", exc)
            time.sleep(max(1.0, poll_interval_sec))
        if last_error:
            raise TimeoutError(f"email OTP timeout after {timeout_sec}s: {last_error}")
        raise TimeoutError(f"email OTP timeout after {timeout_sec}s")

    def wait_for_invite(
        self,
        pickup_url: str,
        *,
        proxy: str,
        email: str = "",
        timeout_sec: float = 180,
        poll_interval_sec: float = 3.0,
    ) -> Optional[str]:
        deadline = time.time() + timeout_sec
        while time.time() < deadline:
            try:
                url = self.find_invite(pickup_url, proxy=proxy, email=email)
                if url:
                    return url
            except Exception as exc:  # noqa: BLE001
                logger.warning("邀请邮件轮询失败: %s", exc)
            time.sleep(max(1.0, poll_interval_sec))
        return None


mail_otp_client = MailOtpClient()


def list_mailbox_codes(
    *,
    email: str,
    pickup_url: str = "",
    proxy: str = "",
    cf_base_url: str = "",
    cf_address: str = "",
    cf_admin_password: str = "",
) -> list[str]:
    from app.services.cloudflare_mail import cloudflare_mail_client

    codes: list[str] = []

    def add(code: Optional[str]) -> None:
        value = str(code or "").strip()
        if value and value not in codes:
            codes.append(value)

    use_cf = bool(cf_base_url and cf_address and cf_admin_password and not pickup_url)
    if use_cf:
        for message in cloudflare_mail_client.fetch_messages(
            base_url=cf_base_url,
            address=cf_address,
            admin_password=cf_admin_password,
            alias=email,
        ):
            add(extract_code("\n".join([message.get("subject", ""), message.get("preview", "")])) or extract_code(message.get("match", "")))
        return codes
    if pickup_url:
        for blob in mail_otp_client.fetch_mailbox(pickup_url, proxy=proxy, email=email):
            add(extract_code(blob))
        return codes
    raise ValueError("未配置邮箱读码方式")


def wait_for_mailbox_item(
    *,
    email: str,
    pickup_url: str = "",
    proxy: str = "",
    kind: str = "code",
    timeout_sec: float = 120,
    poll_interval_sec: float = 3.0,
    cf_base_url: str = "",
    cf_address: str = "",
    cf_admin_password: str = "",
    ignore_values: Optional[set[str]] = None,
) -> Optional[str]:
    from app.services.cloudflare_mail import cloudflare_mail_client

    deadline = time.time() + timeout_sec
    last_error: Optional[Exception] = None
    ignore = {str(item).strip() for item in (ignore_values or set()) if str(item).strip()}
    use_cf = bool(cf_base_url and cf_address and cf_admin_password and not pickup_url)
    while time.time() < deadline:
        try:
            if kind == "code":
                found = next(
                    (
                        code
                        for code in list_mailbox_codes(
                            email=email,
                            pickup_url=pickup_url,
                            proxy=proxy,
                            cf_base_url=cf_base_url,
                            cf_address=cf_address,
                            cf_admin_password=cf_admin_password,
                        )
                        if code not in ignore
                    ),
                    None,
                )
            elif use_cf:
                found = cloudflare_mail_client.find_invite(
                    base_url=cf_base_url,
                    address=cf_address,
                    admin_password=cf_admin_password,
                    alias=email,
                )
            elif pickup_url:
                found = mail_otp_client.find_invite(pickup_url, proxy=proxy, email=email)
            else:
                raise ValueError("未配置邮箱读码方式")
            if found:
                return found
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            logger.warning("邮箱轮询失败: %s", exc)
        time.sleep(max(1.0, poll_interval_sec))
    if kind == "invite":
        return None
    if last_error:
        raise TimeoutError(f"email OTP timeout after {timeout_sec}s: {last_error}")
    raise TimeoutError(f"email OTP timeout after {timeout_sec}s")
