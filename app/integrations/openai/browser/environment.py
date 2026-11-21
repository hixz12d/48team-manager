"""Stable, per-account browser environments shared by signup and OAuth.

Chromium / Chromix: only flags supported by Chromix's published Linux build
(921312ba) are used. Camoufox (camoufox[geoip]==0.5.9): the kernel, GeoIP
database and fpgen model are installed ahead of time and launches never
download; each account keeps one saved fpgen fingerprint.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
from contextlib import contextmanager
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
import time
from typing import Any, Iterator
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import Settings, load_settings

PROFILE_FILE = ".team48-browser.json"
# Ordinary window sizes that fit the existing 1280x900 Xvfb display.
VIEWPORTS = ((1200, 720), (1280, 720), (1200, 800), (1280, 800))

CAMOUFOX_PROFILE_FILE = ".team48-camoufox.json"
CAMOUFOX_SCHEMA = 1
CAMOUFOX_OS = "windows"
# Desktop screens the fingerprint may claim: at least Camoufox's modern floor
# (1366x768, smaller ones get stretched) and fits the 1920x1080 Xvfb display.
CAMOUFOX_SCREEN = {"min_width": 1366, "max_width": 1920, "min_height": 768, "max_height": 1080}
# Firefox writes these into a profile on first start; seeing them without the
# fingerprint manifest means the identity was lost, not that the profile is new.
_FIREFOX_DATA_FILES = {"prefs.js", "places.sqlite", "cookies.sqlite", "times.json", "storage", "cert9.db"}
_LOOPBACK_NO_PROXY = "localhost, 127.0.0.1, ::1"


class BrowserEnvironmentError(ValueError):
    """A configured environment must not silently fall back or change identity."""

    error_code = "browser_environment_invalid"


class ProxyGeoError(BrowserEnvironmentError):
    """Proxy exit timezone unknown; never fall back to the container timezone."""

    error_code = "proxy_geo_failed"


class CamoufoxNotInstalledError(BrowserEnvironmentError):
    """Kernel, GeoIP database or fpgen model missing; never download at launch."""

    error_code = "camoufox_not_installed"


def _proxy_url(proxy_config: dict[str, str]) -> str:
    server = str((proxy_config or {}).get("server") or "").strip()
    username = (proxy_config or {}).get("username")
    if server and username:
        scheme, sep, rest = server.partition("://")
        if sep:
            password = (proxy_config or {}).get("password") or ""
            return f"{scheme}://{quote(str(username), safe='')}:{quote(str(password), safe='')}@{rest}"
    return server


def exit_location(proxy_config: dict[str, str]) -> dict[str, str]:
    """``{"ip", "country", "timezone"}`` of the current proxy exit, looked up through that proxy.

    Looked up on every launch (the local SOCKS bridge port is per-run, so it is not
    a stable cache key). Raises ``ProxyGeoError`` when every provider fails.
    """
    from app.integrations.proxy.geo import GeoLookupError, lookup_exit

    url = _proxy_url(proxy_config)
    if not url:
        raise ProxyGeoError("查询代理出口时区需要代理")
    try:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            exit_info = asyncio.run(lookup_exit(url))
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                exit_info = pool.submit(asyncio.run, lookup_exit(url)).result()
    except GeoLookupError:
        raise ProxyGeoError("经代理查询出口时区失败，已停止（不会退回容器时区）") from None
    timezone = str(exit_info.get("timezone") or "")
    if not timezone:
        raise ProxyGeoError("代理出口时区未知，已停止（不会退回容器时区）")
    try:
        _region("en-US", timezone)
    except BrowserEnvironmentError:
        raise ProxyGeoError("代理出口时区无效，已停止（不会退回容器时区）") from None
    return {"ip": str(exit_info.get("ip") or ""), "country": str(exit_info.get("country") or ""),
            "timezone": timezone}


def exit_timezone(proxy_config: dict[str, str]) -> str:
    """IANA timezone of the current proxy exit (see ``exit_location``)."""
    return exit_location(proxy_config)["timezone"]


def harden_context(context: Any, settings: Settings | None = None) -> None:
    """Hide navigator.webdriver on Playwright Chromium; Chromix handles it natively."""
    engine = (settings or load_settings()).browser_engine
    if engine != "chromium":
        return
    context.add_init_script(
        "Object.defineProperty(Navigator.prototype, 'webdriver', "
        "{get: () => undefined, configurable: true});"
    )


def _region(locale: str, timezone: str) -> None:
    if not isinstance(locale, str) or not re.fullmatch(r"[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*", locale):
        raise BrowserEnvironmentError("浏览器语言配置无效")
    if not isinstance(timezone, str):
        raise BrowserEnvironmentError("浏览器时区配置无效")
    if timezone:
        try:
            ZoneInfo(timezone)
        except (ValueError, ZoneInfoNotFoundError):
            raise BrowserEnvironmentError("浏览器时区必须是有效的 IANA 时区") from None


def _validate(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict) or set(profile) != {
        "schema", "seed", "platform", "locale", "timezone", "viewport",
    }:
        raise BrowserEnvironmentError("浏览器档案格式无效；请恢复原档案，不会自动换指纹")
    if type(profile["schema"]) is not int or profile["schema"] != 1:
        raise BrowserEnvironmentError("浏览器档案版本不支持")
    seed = profile["seed"]
    if type(seed) is not int or not 1 <= seed <= 0xFFFFFFFF:
        raise BrowserEnvironmentError("浏览器档案种子无效；不会自动生成新种子")
    if profile["platform"] != sys.platform:
        raise BrowserEnvironmentError("浏览器档案来自其他操作系统，不能静默切换环境")
    viewport = profile["viewport"]
    if (not isinstance(viewport, dict) or set(viewport) != {"width", "height"}
            or any(type(v) is not int for v in viewport.values())
            or (viewport["width"], viewport["height"]) not in VIEWPORTS):
        raise BrowserEnvironmentError("浏览器档案窗口配置无效")
    _region(profile["locale"], profile["timezone"])
    return profile


def _read_profile(path: Path) -> dict[str, Any]:
    try:
        return _validate(json.loads(path.read_text(encoding="utf-8")))
    except (UnicodeError, json.JSONDecodeError):
        raise BrowserEnvironmentError("浏览器档案损坏；请恢复备份，不会自动换指纹") from None


def persistent_profile(directory: Path, settings: Settings) -> dict[str, Any]:
    """Publish a complete manifest once; concurrent first launches reuse the winner."""
    path = directory / PROFILE_FILE
    try:
        return _read_profile(path)
    except FileNotFoundError:
        pass
    # Never assign a new identity to a browser profile whose manifest was lost.
    if directory.exists() and any(p.name in {"Default", "Local State"} for p in directory.iterdir()):
        raise BrowserEnvironmentError("已有 Chromix 浏览器数据但缺少环境档案，请恢复档案")
    _region(settings.browser_locale, settings.browser_timezone)
    width, height = secrets.choice(VIEWPORTS)
    profile = _validate({
        "schema": 1,
        "seed": secrets.randbits(32) or 1,
        "platform": sys.platform,
        "locale": settings.browser_locale,
        "timezone": settings.browser_timezone or "UTC",
        "viewport": {"width": width, "height": height},
    })
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".team48-browser-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(profile, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        return _read_profile(path)
    finally:
        os.unlink(temporary)


def validate_configuration(settings: Settings, executable_path: str = "") -> None:
    """Check configuration before an invitation or HME claim; no profile writes."""
    if settings.browser_engine not in {"chromium", "chromix", "camoufox"}:
        raise BrowserEnvironmentError("不支持的浏览器引擎")
    _region(settings.browser_locale, settings.browser_timezone)
    if settings.browser_engine == "camoufox":
        camoufox_executable()
    elif settings.browser_engine == "chromix":
        executable = executable_path or settings.browser_executable
        if not executable or not Path(executable).is_file() or Path(executable).suffix.lower() in {".cmd", ".bat", ".zip"}:
            raise BrowserEnvironmentError("Chromix 需要 BROWSER_EXECUTABLE 指向实际浏览器文件")


def context_options(
    profile_dir: Path | str, proxy_config: dict[str, str], *, settings: Settings,
    executable_path: str = "",
) -> dict[str, Any]:
    """Build the Chromium / Chromix launch config (Camoufox: ``launch_browser_context``)."""
    if settings.browser_engine == "camoufox":
        # Never quietly start Chromium when Camoufox is configured.
        raise BrowserEnvironmentError("Camoufox 只能经 launch_browser_context 启动")
    validate_configuration(settings, executable_path)
    engine = settings.browser_engine
    executable = executable_path or settings.browser_executable
    directory = Path(profile_dir)
    options: dict[str, Any] = {
        "user_data_dir": str(directory),
        "headless": settings.browser_headless,
        "proxy": dict(proxy_config),
        "locale": settings.browser_locale,
        "viewport": {"width": 1280, "height": 900},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-features=Translate", "--disable-dev-shm-usage", "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ],
    }
    # Explicit BROWSER_TIMEZONE wins; otherwise follow the proxy exit on every launch.
    timezone = settings.browser_timezone
    if not timezone and _proxy_url(proxy_config):
        timezone = exit_timezone(proxy_config)
    if engine == "chromix":
        # Keep existing Chromium cookies intact and make switching back reversible.
        directory = directory.parent / "chromix" / directory.name
        profile = persistent_profile(directory, settings)
        options.update(user_data_dir=str(directory), locale=profile["locale"], viewport=profile["viewport"])
        # Do not request a made-up platform, GPU, CPU count, RAM or WebRTC IP.
        options["args"] += [
            f"--fingerprint={profile['seed']}",
            "--disable-gpu-fingerprint",
            f"--fingerprint-locale={profile['locale']}",
            f"--lang={profile['locale']}",
        ]
        # The profile keeps seed / locale / window; timezone follows the current exit.
        zone = timezone or profile["timezone"]
        if zone:
            options["timezone_id"] = zone
            options["args"].append(f"--fingerprint-timezone={zone}")
    else:
        _region(settings.browser_locale, timezone)
        if timezone:
            options["timezone_id"] = timezone
    if executable:
        options["executable_path"] = executable
    elif settings.browser_channel:
        options["channel"] = settings.browser_channel
    return options


def environment_summary(options: dict[str, Any]) -> str:
    """A bounded diagnostic without email, paths, proxy credentials or raw seed."""
    seed = next((a.split("=", 1)[1] for a in options.get("args", []) if a.startswith("--fingerprint=")), "")
    viewport = options.get("viewport") or {}
    identity = {"seed": seed, "viewport": viewport, "locale": options.get("locale"),
                "timezone": options.get("timezone_id", "native")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:12]
    engine = "Chromix" if seed else "Chromium"
    return (f"{engine} env={digest} viewport={viewport.get('width', '?')}x{viewport.get('height', '?')} "
            f"locale={options.get('locale', 'native')} timezone={options.get('timezone_id', 'native')}")


# ---------------------------------------------------------------- Camoufox

def camoufox_executable() -> str:
    """Pre-installed Camoufox kernel path; raises instead of downloading anything."""
    try:
        from camoufox.fpgen_model import is_pinned
        from camoufox.geolocation import get_mmdb_path
        from camoufox.pkgman import camoufox_path, launch_path
    except ImportError:
        raise CamoufoxNotInstalledError("未安装 camoufox[geoip]==0.5.9") from None
    try:
        executable = launch_path(camoufox_path(download_if_missing=False))
    except Exception:  # noqa: BLE001 - CamoufoxNotInstalled / UnsupportedVersion
        raise CamoufoxNotInstalledError(
            "Camoufox 浏览器内核未预装（需先运行 python -m camoufox fetch），启动时不会联网下载") from None
    try:
        model_ready = is_pinned()
        geo_ready = get_mmdb_path("ipv4").exists()
    except Exception:  # noqa: BLE001 - missing fpgen package / unreadable GeoIP config
        model_ready = geo_ready = False
    if not model_ready:
        raise CamoufoxNotInstalledError("Camoufox 指纹模型未预装（需先运行 python -m camoufox fetch）")
    if not geo_ready:
        raise CamoufoxNotInstalledError("Camoufox 地理位置库未预装（需先运行 python -m camoufox fetch）")
    return executable


def _mark_geo_db_fresh() -> None:
    """Camoufox re-downloads a GeoIP database it thinks is stale; touch it instead."""
    from camoufox.geolocation import MMDB_DIR

    now = time.time()
    for path in Path(MMDB_DIR).glob("*.mmdb"):
        try:
            os.utime(path, (now, now))
        except OSError:
            pass


def _offline_geolocation(ip: str) -> Any | None:
    """Geolocation of ``ip`` from the local MMDB only; None when it is not listed."""
    from camoufox.exceptions import UnknownIPLocation
    from camoufox.geolocation import get_geolocation

    _mark_geo_db_fresh()
    try:
        location = get_geolocation(ip)
    except UnknownIPLocation:
        return None
    try:
        _region("en-US", str(location.timezone))
    except BrowserEnvironmentError:
        return None
    return location


def camoufox_profile_dir(profile_dir: Path | str) -> Path:
    """``data/chrome-profiles/camoufox/<email>/``; Chromium / Chromix profiles stay untouched."""
    directory = Path(profile_dir)
    return directory.parent / "camoufox" / directory.name


def _validate_camoufox_manifest(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict) or set(data) != {"schema", "os", "fingerprint"}:
        raise BrowserEnvironmentError("Camoufox 指纹档案格式无效；请恢复原档案，不会自动换指纹")
    if type(data["schema"]) is not int or data["schema"] != CAMOUFOX_SCHEMA or data["os"] != CAMOUFOX_OS:
        raise BrowserEnvironmentError("Camoufox 指纹档案版本不支持")
    fingerprint = data["fingerprint"]
    navigator = fingerprint.get("navigator") if isinstance(fingerprint, dict) else None
    screen = fingerprint.get("screen") if isinstance(fingerprint, dict) else None
    user_agent = str((navigator or {}).get("userAgent") or "") if isinstance(navigator, dict) else ""
    if not isinstance(screen, dict) or "Firefox/" not in user_agent or "Windows" not in user_agent:
        raise BrowserEnvironmentError("Camoufox 指纹档案内容无效；请恢复原档案，不会自动换指纹")
    return data


def _read_camoufox_manifest(path: Path) -> dict[str, Any]:
    try:
        return _validate_camoufox_manifest(json.loads(path.read_text(encoding="utf-8")))
    except (UnicodeError, json.JSONDecodeError):
        raise BrowserEnvironmentError("Camoufox 指纹档案损坏；请恢复备份，不会自动换指纹") from None


def _screen_fits(screen: Any) -> bool:
    if not isinstance(screen, dict):
        return False
    width, height = screen.get("width"), screen.get("height")
    return (type(width) is int and type(height) is int
            and CAMOUFOX_SCREEN["min_width"] <= width <= CAMOUFOX_SCREEN["max_width"]
            and CAMOUFOX_SCREEN["min_height"] <= height <= CAMOUFOX_SCREEN["max_height"])


def _generate_camoufox_fingerprint() -> dict[str, Any]:
    """One desktop Windows fpgen fingerprint whose screen fits the 1920x1080 display."""
    from camoufox.fingerprints import Screen, generate_fingerprint

    class _JointScreen(Screen):
        # Screen() gives fpgen one condition per axis and only the last one holds;
        # one joint condition keeps both (same approach as HixBrowser).
        def as_conditions(self) -> dict[str, Any]:
            return {"screen": _screen_fits}

    for _ in range(10):
        try:
            raw = generate_fingerprint(os=CAMOUFOX_OS, screen=_JointScreen(**CAMOUFOX_SCREEN))
        except Exception:  # noqa: BLE001 - fpgen without joint conditions: per-axis bound
            raw = generate_fingerprint(os=CAMOUFOX_OS, screen=Screen(**CAMOUFOX_SCREEN))
        # The saved JSON is what every launch uses, including this first one.
        fingerprint = json.loads(json.dumps(raw, default=str))
        if not _screen_fits(fingerprint.get("screen")):
            continue
        window = fingerprint.get("window")
        # Camoufox draws window.screenY at random on every launch when screenX is
        # outside [-50, 50]; pin the window to the screen corner instead.
        if isinstance(window, dict) and type(window.get("screenX")) is int and not -50 <= window["screenX"] <= 50:
            window["screenX"] = 0
        return fingerprint
    raise BrowserEnvironmentError("Camoufox 生成不出符合 1920x1080 显示器的桌面指纹")


def camoufox_fingerprint(directory: Path) -> dict[str, Any]:
    """The account's saved fpgen fingerprint, created once and never silently replaced.

    Camoufox draws a new identity on every launch unless a fingerprint is passed,
    so the manifest is the identity; a profile that lost it is not re-identified.
    """
    path = directory / CAMOUFOX_PROFILE_FILE
    try:
        return _read_camoufox_manifest(path)["fingerprint"]
    except FileNotFoundError:
        pass
    if directory.exists() and any(p.name in _FIREFOX_DATA_FILES for p in directory.iterdir()):
        raise BrowserEnvironmentError("已有 Camoufox 浏览器数据但缺少指纹档案，请恢复档案")
    manifest = _validate_camoufox_manifest({
        "schema": CAMOUFOX_SCHEMA, "os": CAMOUFOX_OS, "fingerprint": _generate_camoufox_fingerprint(),
    })
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".team48-camoufox-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(manifest, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass
        return _read_camoufox_manifest(path)["fingerprint"]
    finally:
        os.unlink(temporary)


def camoufox_launch_options(
    profile_dir: Path | str, proxy_config: dict[str, str], *, settings: Settings,
) -> tuple[dict[str, Any], Path, str]:
    """``Camoufox(**options)`` kwargs, the Camoufox profile directory and a bounded summary."""
    from camoufox.addons import DefaultAddons

    _region(settings.browser_locale, settings.browser_timezone)
    executable = camoufox_executable()
    directory = camoufox_profile_dir(profile_dir)
    fingerprint = camoufox_fingerprint(directory)
    proxy = dict(proxy_config or {})
    config: dict[str, Any] = {"showcursor": False}
    prefs: dict[str, Any] = {
        # Keep http://127.0.0.1:1455/auth/callback off the proxy so Playwright's route() sees it.
        "network.proxy.no_proxies_on": _LOOPBACK_NO_PROXY,
        "network.proxy.allow_hijacking_localhost": False,
        # Firefox shows a one-off "restore previous session" bar on the second start,
        # which shrinks innerHeight for that launch only.
        "browser.startup.couldRestoreSession.count": 2,
    }
    geoip: str | bool = False
    block_webrtc = True
    timezone = settings.browser_timezone
    if timezone:
        config["timezone"] = timezone
    elif _proxy_url(proxy):
        location = exit_location(proxy)
        exit_ip = location["ip"]
        try:
            is_ipv4 = ipaddress.ip_address(exit_ip).version == 4
        except ValueError:
            is_ipv4 = False
        offline = _offline_geolocation(exit_ip) if is_ipv4 else None
        if offline is not None:
            # Offline MMDB: timezone, coordinates and WebRTC IPv4 follow the exit.
            geoip, block_webrtc, timezone = exit_ip, False, str(offline.timezone)
            prefs["network.dns.disableIPv6"] = True
        else:
            # IPv6 exits (webrtc:ipv6 corrupts the SDP) or IPs the MMDB lacks.
            timezone = location["timezone"]
            config["timezone"] = timezone
    options: dict[str, Any] = {
        "persistent_context": True,
        "user_data_dir": str(directory),
        "executable_path": executable,
        "os": CAMOUFOX_OS,
        "fingerprint": json.loads(json.dumps(fingerprint)),
        "humanize": True,
        "locale": settings.browser_locale,
        "geoip": geoip,
        "block_webrtc": block_webrtc,
        "config": config,
        "firefox_user_prefs": prefs,
        "exclude_addons": [DefaultAddons.UBO],
        # Linux renders as another OS than the identity claims, so the canvas is
        # covered there anyway; saying so everywhere also skips the unproxied
        # host-GPU probe launch on a Windows development machine.
        "canvas_noise": True,
        "headless": bool(settings.browser_headless),
        "i_know_what_im_doing": True,
    }
    if proxy.get("server"):
        options["proxy"] = proxy
    screen = fingerprint.get("screen") or {}
    digest = hashlib.sha256(json.dumps(fingerprint, sort_keys=True).encode()).hexdigest()[:12]
    summary = (f"Camoufox env={digest} screen={screen.get('width', '?')}x{screen.get('height', '?')} "
               f"locale={settings.browser_locale} timezone={timezone or 'native'}")
    return options, directory, summary


@contextmanager
def launch_browser_context(
    profile_dir: Path | str, proxy_config: dict[str, str], *,
    settings: Settings | None = None, executable_path: str = "",
) -> Iterator[tuple[Any, Any, Path, str]]:
    """Launch the configured engine; yields ``(context, page, actual_profile_dir, summary)``.

    Chromium / Chromix keep ``context_options`` + ``harden_context``; Camoufox uses
    the account's saved fingerprint. The context is closed on exit.
    ``executable_path`` only applies to Chromium / Chromix.
    """
    settings = settings or load_settings()
    if settings.browser_engine == "camoufox":
        options, directory, summary = camoufox_launch_options(profile_dir, proxy_config, settings=settings)
        if not settings.browser_headless:
            from app.integrations.openai.browser.onboard import ensure_virtual_display

            ensure_virtual_display()
        from camoufox.sync_api import Camoufox

        with Camoufox(**options) as context:
            page = context.pages[0] if context.pages else context.new_page()
            yield context, page, directory, summary
        return

    from playwright.sync_api import sync_playwright

    options = context_options(profile_dir, proxy_config, settings=settings, executable_path=executable_path)
    summary = environment_summary(options)
    if not settings.browser_headless:
        from app.integrations.openai.browser.onboard import ensure_virtual_display

        ensure_virtual_display()
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(**options)
        try:
            harden_context(context, settings=settings)
            page = context.pages[0] if context.pages else context.new_page()
            yield context, page, Path(options["user_data_dir"]), summary
        finally:
            try:
                context.close()
            except Exception:  # noqa: BLE001 - browser may already be gone
                pass
