"""代理可用性与出口检测。走代理打公开 IP/地理接口，效果接近 IP2Location。"""
from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import httpx

from app.utils.proxy import build_httpx_proxy, mask_proxy_url, normalize_proxy_url

logger = logging.getLogger(__name__)

IP_API_FIELDS = (
    "status,message,query,country,countryCode,regionName,city,zip,"
    "lat,lon,timezone,isp,org,as,mobile,proxy,hosting"
)
IP_API_URL = f"http://ip-api.com/json/?fields={IP_API_FIELDS}"
IPIFY_URL = "https://api.ipify.org?format=json"
IPINFO_URL = "https://ipinfo.io/json"
CHECK_TIMEOUT = 12.0
CHATGPT_PROBE_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"


def _empty_geo() -> Dict[str, Any]:
    return {
        "ip": "",
        "country": "",
        "country_code": "",
        "region": "",
        "city": "",
        "zip": "",
        "lat": None,
        "lon": None,
        "timezone": "",
        "isp": "",
        "org": "",
        "asn": "",
        "mobile": False,
        "proxy_flag": False,
        "hosting": False,
        "source": "",
    }


def _from_ip_api(payload: Dict[str, Any]) -> Dict[str, Any]:
    geo = _empty_geo()
    geo.update({
        "ip": str(payload.get("query") or "").strip(),
        "country": str(payload.get("country") or "").strip(),
        "country_code": str(payload.get("countryCode") or "").strip(),
        "region": str(payload.get("regionName") or "").strip(),
        "city": str(payload.get("city") or "").strip(),
        "zip": str(payload.get("zip") or "").strip(),
        "lat": payload.get("lat"),
        "lon": payload.get("lon"),
        "timezone": str(payload.get("timezone") or "").strip(),
        "isp": str(payload.get("isp") or "").strip(),
        "org": str(payload.get("org") or "").strip(),
        "asn": str(payload.get("as") or "").strip(),
        "mobile": bool(payload.get("mobile")),
        "proxy_flag": bool(payload.get("proxy")),
        "hosting": bool(payload.get("hosting")),
        "source": "ip-api",
    })
    return geo


def _from_ipinfo(payload: Dict[str, Any], ip: str = "") -> Dict[str, Any]:
    loc = str(payload.get("loc") or "")
    lat = lon = None
    if "," in loc:
        parts = loc.split(",", 1)
        try:
            lat = float(parts[0])
            lon = float(parts[1])
        except ValueError:
            lat = lon = None
    geo = _empty_geo()
    geo.update({
        "ip": str(payload.get("ip") or ip or "").strip(),
        "country": str(payload.get("country") or "").strip(),
        "country_code": str(payload.get("country") or "").strip(),
        "region": str(payload.get("region") or "").strip(),
        "city": str(payload.get("city") or "").strip(),
        "zip": str(payload.get("postal") or "").strip(),
        "lat": lat,
        "lon": lon,
        "timezone": str(payload.get("timezone") or "").strip(),
        "isp": str(payload.get("org") or "").strip(),
        "org": str(payload.get("org") or "").strip(),
        "asn": str(payload.get("org") or "").strip(),
        "source": "ipinfo",
    })
    return geo


async def _fetch_json(client: httpx.AsyncClient, url: str) -> Dict[str, Any]:
    response = await client.get(url)
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"{url} 返回了非 JSON 对象")
    return payload


async def _lookup_egress(client: httpx.AsyncClient) -> Dict[str, Any]:
    try:
        payload = await _fetch_json(client, IP_API_URL)
        if str(payload.get("status") or "").lower() == "success" and payload.get("query"):
            return _from_ip_api(payload)
        raise ValueError(payload.get("message") or "ip-api 查询失败")
    except Exception as first_error:
        logger.info("ip-api 探测失败，改用 ipify/ipinfo: %s", first_error)
        ip = ""
        try:
            ip = str((await _fetch_json(client, IPIFY_URL)).get("ip") or "").strip()
        except Exception:
            ip = ""
        payload = await _fetch_json(client, IPINFO_URL)
        geo = _from_ipinfo(payload, ip=ip)
        if not geo["ip"]:
            raise ValueError("无法获取出口 IP")
        return geo


def _tcp_probe(host: str, port: int, timeout: float = 5.0) -> Dict[str, Any]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {
                "ok": True,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "error": "",
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "error": str(exc),
        }


def _probe_chatgpt(proxy: str) -> Dict[str, Any]:
    from curl_cffi.requests import Session

    from app.utils.proxy import build_curl_cffi_proxies

    started = time.perf_counter()
    try:
        with Session(
            impersonate="chrome136",
            proxies=build_curl_cffi_proxies(proxy),
            timeout=12,
            verify=False,
        ) as session:
            response = session.get(
                CHATGPT_PROBE_URL,
                headers={"Accept": "*/*", "Referer": "https://chatgpt.com/"},
            )
            cf_mitigated = response.headers.get("cf-mitigated") or ""
            content_type = response.headers.get("content-type") or ""
            text = response.text or ""
            looks_like_challenge = (
                cf_mitigated == "challenge"
                or "just a moment" in text.lower()
                or (response.status_code == 403 and "text/html" in content_type)
            )
            # 未带 Token 时 401 JSON 也算通了 ChatGPT，说明 TLS/CF 没拦。
            ok = (not looks_like_challenge) and (
                response.status_code in {200, 401}
                or (200 <= response.status_code < 500 and "application/json" in content_type)
            )
            return {
                "ok": ok,
                "status": response.status_code,
                "latency_ms": int((time.perf_counter() - started) * 1000),
                "cf_mitigated": cf_mitigated,
                "error": "" if ok else f"ChatGPT 返回 {response.status_code}",
            }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "status": 0,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "cf_mitigated": "",
            "error": str(exc),
        }


async def check_proxy(proxy: Optional[str], *, compare_direct: bool = True) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "ok": False,
        "proxy": "",
        "proxy_host": "",
        "proxy_port": None,
        "scheme": "",
        "latency_ms": None,
        "tcp_ok": False,
        "tcp_latency_ms": None,
        "chatgpt_ok": False,
        "chatgpt_status": None,
        "egress": _empty_geo(),
        "direct": _empty_geo() if compare_direct else None,
        "same_as_host": False,
        "summary": "",
        "error": "",
    }
    try:
        normalized = normalize_proxy_url(proxy)
    except ValueError as exc:
        result["error"] = str(exc)
        result["summary"] = "代理格式无效"
        return result

    if not normalized:
        result["error"] = "未填写代理"
        result["summary"] = "未填写代理"
        return result

    parsed = urlparse(normalized)
    result["proxy"] = mask_proxy_url(normalized)
    result["proxy_host"] = parsed.hostname or ""
    result["proxy_port"] = parsed.port
    result["scheme"] = parsed.scheme

    tcp = await asyncio.to_thread(_tcp_probe, parsed.hostname or "", int(parsed.port or 0))
    result["tcp_ok"] = bool(tcp["ok"])
    result["tcp_latency_ms"] = tcp["latency_ms"]
    if not tcp["ok"]:
        result["error"] = f"TCP 连不上代理: {tcp['error']}"
        result["summary"] = "代理端口不通"
        return result

    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            timeout=CHECK_TIMEOUT,
            follow_redirects=True,
            proxy=build_httpx_proxy(normalized),
        ) as client:
            result["egress"] = await _lookup_egress(client)
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
    except Exception as exc:  # noqa: BLE001
        result["latency_ms"] = int((time.perf_counter() - started) * 1000)
        result["error"] = f"走代理出网失败: {exc}"
        result["summary"] = "代理能连上，但出不了网"
        return result

    if compare_direct:
        try:
            async with httpx.AsyncClient(timeout=CHECK_TIMEOUT, follow_redirects=True) as client:
                result["direct"] = await _lookup_egress(client)
        except Exception as exc:  # noqa: BLE001
            logger.info("本机出口探测失败: %s", exc)
            result["direct"] = _empty_geo()

    egress_ip = result["egress"].get("ip") or ""
    direct_ip = (result.get("direct") or {}).get("ip") or ""
    result["same_as_host"] = bool(egress_ip and direct_ip and egress_ip == direct_ip)
    result["ok"] = bool(egress_ip) and not result["same_as_host"]

    place = " / ".join(part for part in [
        result["egress"].get("country"),
        result["egress"].get("region"),
        result["egress"].get("city"),
    ] if part)
    isp = result["egress"].get("isp") or result["egress"].get("org") or ""
    bits = [f"出口 {egress_ip}"]
    if place:
        bits.append(place)
    if isp:
        bits.append(isp)
    if result["egress"].get("hosting"):
        bits.append("机房/托管 IP")
    if result["same_as_host"]:
        result["error"] = "走代理后的出口 IP 和 VPS 本机一样，这条代理没有真正改出口"
        result["summary"] = "代理未生效"
        return result
    chatgpt = await asyncio.to_thread(_probe_chatgpt, normalized)
    result["chatgpt_ok"] = bool(chatgpt.get("ok"))
    result["chatgpt_status"] = chatgpt.get("status")
    if not chatgpt.get("ok"):
        result["ok"] = False
        result["error"] = f"出口正常，但打不通 chatgpt.com: {chatgpt.get('error') or '未知错误'}"
        result["summary"] = "代理出网正常，但 ChatGPT 不通"
        return result

    bits.append("ChatGPT 可通")
    result["summary"] = " · ".join(bits)
    return result


async def check_many(items: list[Dict[str, Any]]) -> list[Dict[str, Any]]:
    results = []
    for item in items:
        checked = await check_proxy(item.get("proxy") or "", compare_direct=True)
        checked["label"] = item.get("label") or ""
        checked["kind"] = item.get("kind") or ""
        checked["id"] = item.get("id")
        results.append(checked)
    return results
