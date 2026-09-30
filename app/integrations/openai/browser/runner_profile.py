"""Per-account Chromix persona for the extension runner, and its launch arguments.

Separate from ``environment.py`` (``.team48-browser.json``): runner profiles live in
``data/chrome-profiles/runner/<email>/`` and are never migrated from old profiles.

Flags were checked against the Chromix Linux x64 build published as v153.0.8010.36
(source 5b44246f, ``patches/0036`` normalizer). That build has no public
``--fingerprint-gpu-backend`` alias, so the GPU backend policy is set through the
underlying ``--uxr-gpu-backend`` switch, which newer builds accept as well.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.integrations.openai.browser.environment import BrowserEnvironmentError

PROFILE_FILE = ".team48-runner.json"
SCHEMA = 1
PLATFORMS = ("linux", "windows")
GPU_MODES = ("native", "preset")
LOCALE = "en-US"
# Chromix turns --fingerprint-locale into both navigator.languages and Accept-Language.
# A real en-US Chrome sends "en-US,en"; a lone "en-US" list is itself unusual.
LANGUAGES = "en-US,en"
SCREEN = {"width": 1920, "height": 1080}
WINDOWS = ((1600, 900), (1680, 960), (1760, 990))
HARDWARE_CONCURRENCY = (4, 8, 12, 16)
# Real Chrome caps navigator.deviceMemory at 8, so a larger value would itself stand out.
DEVICE_MEMORY = (8,)
# Loopback stays direct: the extension talks to Team48 on 127.0.0.1 without the proxy.
PROXY_BYPASS = "localhost;127.0.0.1;[::1]"

# Chrome 153 reports WebGL through ANGLE on both platforms. Complete vendor/renderer pairs
# are shown verbatim in compatibility mode; Chromix keeps the native identity on software
# (SwiftShader / llvmpipe) contexts regardless of these strings.
PRESET_GPUS: dict[str, tuple[tuple[str, str], ...]] = {
    "windows": (
        ("Google Inc. (Intel)",
         "ANGLE (Intel, Intel(R) UHD Graphics 630 (0x00003E92) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (Intel)",
         "ANGLE (Intel, Intel(R) UHD Graphics 770 (0x0000A780) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
        ("Google Inc. (NVIDIA)",
         "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 (0x00002504) Direct3D11 vs_5_0 ps_5_0, D3D11)"),
    ),
    "linux": (
        ("Google Inc. (Intel)",
         "ANGLE (Intel, Mesa Intel(R) UHD Graphics 630 (CFL GT2), OpenGL 4.6)"),
    ),
}

_KEYS = {
    "schema", "seed", "platform", "gpu", "locale", "timezone", "screen", "window",
    "hardware_concurrency", "device_memory", "exit_country", "exit_ip_hash",
}


class RunnerEnvironmentError(BrowserEnvironmentError):
    """A runner precondition failed; ``error_code`` is one of the runner codes."""

    def __init__(self, error_code: str, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code


def _invalid(message: str) -> RunnerEnvironmentError:
    return RunnerEnvironmentError("runner_profile_invalid", message)


def runner_profiles_root() -> Path:
    return Path(__file__).resolve().parents[4] / "data" / "chrome-profiles" / "runner"


def runner_profile_dir(email: str) -> Path:
    name = str(email or "").strip().lower().replace("@", "_at_")
    if not re.fullmatch(r"[a-z0-9._+-]+_at_[a-z0-9.-]+", name):
        raise RunnerEnvironmentError("runner_not_configured", "运行器档案需要有效邮箱")
    return runner_profiles_root() / name


def exit_ip_hash(ip: str) -> str:
    return hashlib.sha256(str(ip).strip().encode("utf-8")).hexdigest()[:12]


def valid_timezone(value: Any) -> bool:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){1,2}", value):
        return False
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError):
        return False
    return True


def _preset_gpu(platform: str, seed: int) -> dict[str, str]:
    options = PRESET_GPUS[platform]
    vendor, renderer = options[seed % len(options)]
    return {"vendor": vendor, "renderer": renderer}


def _validate(profile: Any) -> dict[str, Any]:
    if not isinstance(profile, dict) or set(profile) != _KEYS:
        raise _invalid("运行器档案格式无效；请恢复原档案，不会自动换指纹")
    if type(profile["schema"]) is not int or profile["schema"] != SCHEMA:
        raise _invalid("运行器档案版本不支持")
    seed = profile["seed"]
    if type(seed) is not int or not 1 <= seed <= 0xFFFFFFFF:
        raise _invalid("运行器档案种子无效；不会自动生成新种子")
    if profile["platform"] not in PLATFORMS:
        raise _invalid("运行器档案伪装平台无效")
    gpu = profile["gpu"]
    if gpu != "native" and not (
            isinstance(gpu, dict) and set(gpu) == {"vendor", "renderer"}
            and (gpu["vendor"], gpu["renderer"]) in PRESET_GPUS[profile["platform"]]):
        raise _invalid("运行器档案显卡配置无效")
    if profile["locale"] != LOCALE:
        raise _invalid("运行器档案语言无效")
    if not valid_timezone(profile["timezone"]):
        raise _invalid("运行器档案时区不是有效的 IANA 时区")
    if profile["screen"] != SCREEN:
        raise _invalid("运行器档案屏幕配置无效")
    window = profile["window"]
    if (not isinstance(window, dict) or set(window) != {"width", "height"}
            or any(type(v) is not int for v in window.values())
            or (window["width"], window["height"]) not in WINDOWS):
        raise _invalid("运行器档案窗口配置无效")
    if type(profile["hardware_concurrency"]) is not int or profile["hardware_concurrency"] not in HARDWARE_CONCURRENCY:
        raise _invalid("运行器档案 CPU 核数无效")
    if type(profile["device_memory"]) is not int or profile["device_memory"] not in DEVICE_MEMORY:
        raise _invalid("运行器档案内存配置无效")
    if not isinstance(profile["exit_country"], str) or not re.fullmatch(r"[A-Z]{2}", profile["exit_country"]):
        raise _invalid("运行器档案出口国家无效")
    if not isinstance(profile["exit_ip_hash"], str) or not re.fullmatch(r"[0-9a-f]{12}", profile["exit_ip_hash"]):
        raise _invalid("运行器档案出口 IP 摘要无效")
    return profile


def _read(path: Path) -> dict[str, Any]:
    try:
        return _validate(json.loads(path.read_text(encoding="utf-8")))
    except (UnicodeError, json.JSONDecodeError):
        raise _invalid("运行器档案损坏；请恢复备份，不会自动换指纹") from None


def load_runner_profile(directory: Path | str) -> dict[str, Any] | None:
    """Existing profile, ``None`` when never created; corrupt or invalid raises."""
    try:
        return _read(Path(directory) / PROFILE_FILE)
    except FileNotFoundError:
        return None


def create_runner_profile(
    directory: Path | str, *, geo: dict[str, Any] | None, platform: str, gpu_mode: str,
) -> dict[str, Any]:
    """Publish a complete profile once; a concurrent first creation reuses the winner.

    ``geo`` is the result of ``geo.lookup_exit`` for this account's proxy. Without a
    verified timezone the profile is not created (no Shanghai / UTC fallback).
    """
    directory = Path(directory)
    path = directory / PROFILE_FILE
    existing = load_runner_profile(directory)
    if existing is not None:
        return existing
    # Never give a new identity to a browser profile whose manifest was lost.
    if directory.exists() and any(p.name in {"Default", "Local State"} for p in directory.iterdir()):
        raise _invalid("已有运行器浏览器数据但缺少档案，请恢复档案")
    if platform not in PLATFORMS:
        raise RunnerEnvironmentError("runner_not_configured", "RUNNER_FINGERPRINT_PLATFORM 只能是 linux 或 windows")
    if gpu_mode not in GPU_MODES:
        raise RunnerEnvironmentError("runner_not_configured", "RUNNER_GPU_MODE 只能是 native 或 preset")
    geo = geo if isinstance(geo, dict) else {}
    timezone = geo.get("timezone")
    country = str(geo.get("country") or "").upper()
    ip = str(geo.get("ip") or "")
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        ip = ""
    if not valid_timezone(timezone) or not re.fullmatch(r"[A-Z]{2}", country) or not ip:
        raise RunnerEnvironmentError("runner_geo_unknown", "未能经代理查到出口 IP 所在时区，未创建浏览器档案")
    seed = secrets.randbits(32) or 1
    width, height = secrets.choice(WINDOWS)
    profile = _validate({
        "schema": SCHEMA,
        "seed": seed,
        "platform": platform,
        "gpu": _preset_gpu(platform, seed) if gpu_mode == "preset" else "native",
        "locale": LOCALE,
        "timezone": timezone,
        "screen": dict(SCREEN),
        "window": {"width": width, "height": height},
        "hardware_concurrency": secrets.choice(HARDWARE_CONCURRENCY),
        "device_memory": secrets.choice(DEVICE_MEMORY),
        "exit_country": country,
        "exit_ip_hash": exit_ip_hash(ip),
    })
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".team48-runner-", dir=directory)
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
        return _read(path)
    finally:
        os.unlink(temporary)


def _proxy_arg(proxy_server: str) -> str:
    """Chromium proxy URL without credentials; socks5h is spelled socks5 (remote DNS)."""
    parsed = urlsplit(str(proxy_server or "").strip())
    scheme = "socks5" if parsed.scheme in {"socks5", "socks5h"} else parsed.scheme
    if parsed.username or parsed.password or "@" in parsed.netloc:
        raise RunnerEnvironmentError("proxy_auth_unsupported", "Chromix 子进程不能直接使用带账号密码的代理")
    try:
        port = parsed.port
    except ValueError:
        port = None
    if scheme not in {"http", "https", "socks5"} or not parsed.hostname or not port:
        raise RunnerEnvironmentError("runner_launch_failed", "浏览器代理格式无效")
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return f"{scheme}://{host}:{port}"


def chromix_args(
    profile: dict[str, Any], *, user_data_dir: Path | str, extension_dir: Path | str,
    proxy_server: str, bypass: str = PROXY_BYPASS,
) -> list[str]:
    """Complete Chromix command line (without the executable) for one runner launch.

    No remote debugging port, automation switch, headless mode or Playwright flags.
    """
    profile = _validate(profile)
    extension = str(Path(extension_dir).resolve())
    window = profile["window"]
    args = [
        f"--user-data-dir={Path(user_data_dir).resolve()}",
        f"--load-extension={extension}",
        f"--disable-extensions-except={extension}",
        f"--proxy-server={_proxy_arg(proxy_server)}",
        f"--proxy-bypass-list={bypass}",
        f"--fingerprint={profile['seed']}",
        f"--fingerprint-platform={profile['platform']}",
        f"--fingerprint-timezone={profile['timezone']}",
        f"--fingerprint-locale={LANGUAGES}",
        f"--lang={profile['locale']}",
        f"--fingerprint-hardware-concurrency={profile['hardware_concurrency']}",
        f"--fingerprint-device-memory={profile['device_memory']}",
        f"--fingerprint-screen-width={profile['screen']['width']}",
        f"--fingerprint-screen-height={profile['screen']['height']}",
    ]
    if profile["platform"] == "windows":
        # Align font metrics with the Windows fonts shipped in the Chromix Linux bundle.
        args.append("--fingerprint-windows-font-metrics")
    gpu = profile["gpu"]
    if gpu == "native":
        args.append("--uxr-gpu-backend=native")
    else:
        args += [
            "--uxr-gpu-backend=compatibility",
            f"--fingerprint-gpu-vendor={gpu['vendor']}",
            f"--fingerprint-gpu-renderer={gpu['renderer']}",
        ]
    args += [
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
        f"--window-size={window['width']},{window['height']}",
        "--window-position=0,0",
        "--no-first-run",
        "--no-default-browser-check",
        "--password-store=basic",
        "--disable-features=Translate,DisableLoadExtensionCommandLineSwitch",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "--silent-debugger-extension-api",
        # Docker's default 64 MB /dev/shm crashes renderers; not visible to pages.
        "--disable-dev-shm-usage",
        # The container runs as root.
        "--no-sandbox",
    ]
    forbidden = ("--remote-debugging", "--enable-automation", "--headless", "--playwright")
    if any(arg.startswith(forbidden) for arg in args):
        raise RunnerEnvironmentError("runner_launch_failed", "运行器启动参数包含自动化开关")
    return args


def profile_summary(profile: dict[str, Any]) -> str:
    """Bounded summary without seed, email, paths or proxy details."""
    profile = _validate(profile)
    digest = hashlib.sha256(json.dumps(profile, sort_keys=True).encode("utf-8")).hexdigest()[:12]
    window = profile["window"]
    gpu = "原生" if profile["gpu"] == "native" else "预设"
    return (f"Chromix 扩展运行器 平台={profile['platform']} 时区={profile['timezone']} "
            f"窗口={window['width']}x{window['height']} 显卡={gpu} "
            f"出口={profile['exit_country']} 档案={digest}")
