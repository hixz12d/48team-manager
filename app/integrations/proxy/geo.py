"""Exit IP, country and IANA timezone of a proxy, looked up through that proxy.

Used when a runner profile is first created so the browser timezone matches the
exit location. No fallback: if every provider fails the caller must stop.
Proxy credentials are never logged or put into error messages.
"""
from __future__ import annotations

import ipaddress
import logging
import re
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx

from app.core.proxy import normalize_proxy_url

logger = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10.0
MAX_BYTES = 64 * 1024
PROVIDERS = (
    ("ipinfo", "https://ipinfo.io/json"),
    ("ipapi", "https://ipapi.co/json/"),
)


class GeoLookupError(RuntimeError):
    """Every provider failed; the runner reports ``runner_geo_unknown``."""

    error_code = "runner_geo_unknown"


def _parse(data: Any) -> dict[str, str] | None:
    if not isinstance(data, dict):
        return None
    ip = str(data.get("ip") or "").strip()
    country = str(data.get("country_code") or data.get("country") or "").strip().upper()
    timezone = str(data.get("timezone") or "").strip()
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        return None
    if not re.fullmatch(r"[A-Z]{2}", country) or not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){1,2}", timezone):
        return None
    try:
        ZoneInfo(timezone)
    except (ValueError, ZoneInfoNotFoundError):
        return None
    return {"ip": ip, "country": country, "timezone": timezone}


async def lookup_exit(proxy_url: str) -> dict[str, str]:
    """``{"ip", "country", "timezone"}`` of the proxy exit; raises ``GeoLookupError``.

    Accepts http(s) and socks5/socks5h proxies, with or without credentials
    (``socksio`` provides SOCKS support for httpx). Never connects directly.
    """
    try:
        proxy = normalize_proxy_url(proxy_url)
    except ValueError:
        proxy = None
    if not proxy:
        raise GeoLookupError("出口查询需要有效代理")
    async with httpx.AsyncClient(proxy=proxy, timeout=TIMEOUT_SECONDS, follow_redirects=False,
                                 trust_env=False, headers={"Accept": "application/json"}) as client:
        for name, url in PROVIDERS:
            try:
                response = await client.get(url)
                if response.status_code != 200 or len(response.content) > MAX_BYTES:
                    logger.info("exit geo provider %s returned HTTP %s", name, response.status_code)
                    continue
                result = _parse(response.json())
            except Exception:  # noqa: BLE001 - proxy/TLS/JSON errors all mean "try the next provider"
                # Exception text may contain the proxy URL; log only the provider and type.
                logger.info("exit geo provider %s failed", name)
                continue
            if result:
                return result
            logger.info("exit geo provider %s returned incomplete data", name)
    raise GeoLookupError("经代理查询出口 IP 所在地失败")
