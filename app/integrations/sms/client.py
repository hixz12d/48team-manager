"""接码客户端。格式: +1xxxxxxxxxx----https://api668.com/sms/by_key?key=..."""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.core.proxy import build_httpx_proxy, normalize_proxy_url

logger = logging.getLogger(__name__)

CODE_RE = re.compile(r"\b(\d{6})\b")


def parse_phone_line(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    if "----" in raw:
        number, sms_url = raw.split("----", 1)
        return number.strip(), sms_url.strip()
    if raw.startswith("http"):
        return "", raw
    return raw, ""


def require_proxy(proxy: Optional[str], purpose: str) -> str:
    normalized = normalize_proxy_url(proxy)
    if not normalized:
        raise ValueError(f"{purpose} 必须配置静态 ISP 代理")
    return normalized


class SmsClient:
    def __init__(self, *, timeout: float = 20.0) -> None:
        self.timeout = timeout

    def _extract(self, obj: Any) -> Optional[str]:
        if obj is None:
            return None
        if isinstance(obj, str):
            match = CODE_RE.search(obj)
            return match.group(1) if match else None
        if isinstance(obj, (int, float)):
            text = str(int(obj))
            return text if re.fullmatch(r"\d{4,8}", text) else None
        if isinstance(obj, dict):
            if isinstance(obj.get("messages"), list):
                for item in obj["messages"]:
                    found = self._extract(item)
                    if found:
                        return found
            for key in ("code", "sms_code", "otp", "verification_code", "data", "msg", "message", "text", "content"):
                if key in obj:
                    found = self._extract(obj[key])
                    if found:
                        return found
            for value in obj.values():
                found = self._extract(value)
                if found:
                    return found
        if isinstance(obj, list):
            for item in obj:
                found = self._extract(item)
                if found:
                    return found
        return None

    def fetch_code(self, sms_url: str, *, proxy: str) -> Optional[str]:
        normalized_proxy = require_proxy(proxy, "接码")
        if not str(sms_url or "").startswith("http"):
            raise ValueError("接码 URL 无效")
        with httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            proxy=build_httpx_proxy(normalized_proxy),
        ) as client:
            response = client.get(sms_url)
            text = response.text.strip()
        if not text:
            return None
        try:
            return self._extract(json.loads(text))
        except json.JSONDecodeError:
            match = CODE_RE.search(text)
            return match.group(1) if match else None

    def wait_for_code(
        self,
        sms_url: str,
        *,
        proxy: str,
        timeout_sec: float = 90,
        poll_interval_sec: float = 1.5,
    ) -> str:
        deadline = time.time() + timeout_sec
        last_error: Optional[Exception] = None
        while time.time() < deadline:
            try:
                code = self.fetch_code(sms_url, proxy=proxy)
                if code:
                    return code
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                logger.warning("接码轮询失败: %s", exc)
            time.sleep(max(0.5, poll_interval_sec))
        if last_error:
            raise TimeoutError(f"SMS OTP timeout after {timeout_sec}s: {last_error}")
        raise TimeoutError(f"SMS OTP timeout after {timeout_sec}s")


sms_client = SmsClient()


def chrome_proxy_config(proxy: Optional[str]) -> dict[str, str]:
    normalized = require_proxy(proxy, "浏览器")
    parsed = urlparse(normalized)
    host = parsed.hostname or ""
    port = parsed.port
    if not host or not port:
        raise ValueError("浏览器代理缺少 host/port")
    scheme = "socks5" if parsed.scheme.startswith("socks5") else parsed.scheme
    config = {"server": f"{scheme}://{host}:{port}"}
    if parsed.username is not None:
        from urllib.parse import unquote
        config["username"] = unquote(parsed.username)
        if parsed.password is not None:
            config["password"] = unquote(parsed.password)
    return config
