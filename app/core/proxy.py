"""Proxy URL helpers. Credentials never belong in query payloads."""

from __future__ import annotations

from urllib.parse import quote, unquote, urlparse, urlunparse

SUPPORTED_PROXY_SCHEMES = ("http", "https", "socks5", "socks5h")


def normalize_proxy_url(proxy: str | None) -> str | None:
    value = str(proxy or "").strip()
    if not value:
        return None
    if "://" not in value:
        parts = value.split(":")
        if len(parts) == 2:
            value = f"socks5h://{parts[0]}:{parts[1]}"
        elif len(parts) == 4:
            host, port, username, password = parts
            value = (
                f"socks5h://{quote(username, safe='')}:{quote(password, safe='')}"
                f"@{host}:{port}"
            )
        else:
            raise ValueError("proxy url must be scheme://host:port or host:port:user:pass")
    parsed = urlparse(value)
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("proxy url host/port is invalid") from exc
    if parsed.scheme not in SUPPORTED_PROXY_SCHEMES or not parsed.netloc or not parsed.hostname:
        raise ValueError("proxy url scheme must be http, https, socks5, or socks5h")
    if parsed.scheme == "socks5":
        value = urlunparse(parsed._replace(scheme="socks5h"))
    return value


def build_curl_cffi_proxies(proxy: str | None) -> dict[str, str] | None:
    normalized = normalize_proxy_url(proxy)
    if not normalized:
        return None
    return {"all": normalized, "http": normalized, "https": normalized}


def mask_proxy_url(proxy: str | None) -> str:
    try:
        normalized = normalize_proxy_url(proxy)
    except ValueError:
        return "invalid"
    if not normalized:
        return ""
    parsed = urlparse(normalized)
    netloc = parsed.netloc
    if parsed.username is not None:
        credentials = "***:***" if parsed.password is not None else "***"
        host = parsed.hostname or ""
        if parsed.port:
            host = f"{host}:{parsed.port}"
        netloc = f"{credentials}@{host}"
    return urlunparse(parsed._replace(netloc=netloc))


def split_proxy_url(url: str) -> dict:
    normalized = normalize_proxy_url(url)
    if not normalized:
        raise ValueError("proxy url is empty")
    parsed = urlparse(normalized)
    if not parsed.hostname or parsed.port is None:
        raise ValueError("proxy url is missing host/port")
    return {
        "url": normalized,
        "scheme": parsed.scheme,
        "host": parsed.hostname,
        "port": int(parsed.port),
        "username": unquote(parsed.username) if parsed.username is not None else "",
        "password": unquote(parsed.password) if parsed.password is not None else "",
    }


def compose_proxy_url(*, scheme: str, host: str, port: int, username: str = "", password: str = "") -> str:
    netloc = f"{host}:{int(port)}"
    if username:
        user = quote(username, safe="")
        if password:
            user = f"{user}:{quote(password, safe='')}"
        netloc = f"{user}@{netloc}"
    return urlunparse((scheme, netloc, "", "", "", ""))


def inherit_proxy_url(*candidates: str | None) -> str:
    for item in candidates:
        text = str(item or "").strip()
        if not text:
            continue
        try:
            return normalize_proxy_url(text) or text
        except ValueError:
            return text
    return ""
