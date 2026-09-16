"""Stable, per-account browser environments shared by signup and OAuth.

Only flags supported by Chromix's published Linux build (921312ba) are used.
No SDK downloads, proxy discovery or synthetic OS/GPU/hardware pools are needed.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import tempfile
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import Settings

PROFILE_FILE = ".team48-browser.json"
# Ordinary window sizes that fit the existing 1280x900 Xvfb display.
VIEWPORTS = ((1200, 720), (1280, 720), (1200, 800), (1280, 800))


class BrowserEnvironmentError(ValueError):
    """A configured environment must not silently fall back or change identity."""

    error_code = "browser_environment_invalid"


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
    if settings.browser_engine not in {"chromium", "chromix"}:
        raise BrowserEnvironmentError("不支持的浏览器引擎")
    _region(settings.browser_locale, settings.browser_timezone)
    if settings.browser_engine == "chromix":
        executable = executable_path or settings.browser_executable
        if not executable or not Path(executable).is_file() or Path(executable).suffix.lower() in {".cmd", ".bat", ".zip"}:
            raise BrowserEnvironmentError("Chromix 需要 BROWSER_EXECUTABLE 指向实际浏览器文件")


def context_options(
    profile_dir: Path | str, proxy_config: dict[str, str], *, settings: Settings,
    executable_path: str = "",
) -> dict[str, Any]:
    """Build the complete launch config for every browser entry point."""
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
        ],
    }
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
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        ]
        if profile["timezone"]:
            options["timezone_id"] = profile["timezone"]
            options["args"].append(f"--fingerprint-timezone={profile['timezone']}")
    else:
        _region(settings.browser_locale, settings.browser_timezone)
        if settings.browser_timezone:
            options["timezone_id"] = settings.browser_timezone
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
