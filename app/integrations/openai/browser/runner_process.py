"""Chromix subprocess for the extension runner: run directory, display, proxy bridge, launch, shutdown.

No Playwright and no CDP: the browser is a plain child process started with the real
signup extension; the extension talks to Team48 over loopback HTTP.
Protocol: ``docs/contracts/extension-runner.md``.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import time
from typing import Any
from urllib.parse import unquote, urlsplit

from app.core.config import BASE_DIR
from app.core.proxy import normalize_proxy_url
from app.integrations.openai.browser.runner_profile import RunnerEnvironmentError
from app.integrations.proxy.socks_bridge import SocksAuthBridge

logger = logging.getLogger(__name__)

RUNS_ROOT = BASE_DIR / "data" / "runner-runs"
PRIVATE_CONFIG = "private-config.mjs"
RUNNER_CONFIG = "runner-config.json"
LOG_NAME = "browser.log"
EXTENSION_DIR = "extension"
DEFAULT_DISPLAY = ":99"
XVFB_SCREEN = "1920x1080x24"
LOG_TAIL_LINES = 50
_RUN_ID = re.compile(r"^[a-f0-9]{32}$")
# Never on the Chromix command line: remote debugging, automation mode, headless, Playwright.
FORBIDDEN_FLAGS = ("--remote-debugging", "--enable-automation", "--headless", "--playwright")
# Chromium only honours these when --proxy-server is absent; dropped anyway so nothing bypasses the proxy.
_PROXY_ENV = {"http_proxy", "https_proxy", "all_proxy", "no_proxy", "ftp_proxy", "socks_proxy"}
# The browser child gets a minimal environment on POSIX (no app secrets, no Playwright paths).
_POSIX_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LANGUAGE", "LC_ALL", "LC_CTYPE",
              "TMPDIR", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XAUTHORITY",
              "DBUS_SESSION_BUS_ADDRESS")
_SECRET_ENV = re.compile(r"(SECRET|PASSWORD|TOKEN|_KEY$|^DATABASE_URL$|^ADMIN_|^SUB2API|^PLAYWRIGHT)", re.I)
_URL_QUERY = re.compile(r"(\b[a-z][a-z0-9+.-]*://[^\s?#\"'<>]+)[?#][^\s\"'<>]*", re.I)
_EMAIL = re.compile(r"[^\s@\"'<>]+@[^\s@\"'<>]+\.[A-Za-z]{2,}")
_SINGLETON_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")


def _launch_error(message: str) -> RunnerEnvironmentError:
    return RunnerEnvironmentError("runner_launch_failed", message)


def new_run_id() -> str:
    return secrets.token_hex(16)


def new_token() -> str:
    return secrets.token_urlsafe(32)


@dataclass
class RunDir:
    root: Path
    extension: Path
    log: Path


def origin_of(url: str) -> str:
    """``scheme://host[:port]`` of an http(s) URL, or ``""``."""
    parsed = urlsplit(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return ""
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        return ""
    return f"{parsed.scheme}://{host}:{port}" if port else f"{parsed.scheme}://{host}"


def _private_config(*, mailbox: dict[str, str], runner_base_url: str, run_id: str, token: str) -> str:
    mailbox_js = json.dumps({
        "baseUrl": str(mailbox.get("base_url") or ""),
        "address": str(mailbox.get("address") or ""),
        "adminPassword": str(mailbox.get("admin_password") or ""),
    }, ensure_ascii=True)
    runner_js = json.dumps({"baseUrl": runner_base_url, "runId": run_id, "token": token}, ensure_ascii=True)
    return ("// Generated per run by Team48; deleted when the run ends.\n"
            f"export const MAILBOX = Object.freeze({mailbox_js});\n"
            f"export const RUNNER = Object.freeze({runner_js});\n")


def _rewrite_manifest(path: Path, *, kind: str, runner_base_url: str, mailbox_base_url: str) -> None:
    manifest = json.loads(path.read_text(encoding="utf-8"))
    permissions = [str(item) for item in manifest.get("host_permissions") or []]
    wanted = [origin_of(runner_base_url) + "/*"]
    mailbox_origin = origin_of(mailbox_base_url)
    if mailbox_origin:
        wanted.append(mailbox_origin + "/*")
    if kind == "selfcheck":
        wanted.append("<all_urls>")
    for pattern in wanted:
        if pattern not in permissions:
            permissions.append(pattern)
    manifest["host_permissions"] = permissions
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def prepare_run_dir(
    run_id: str, *, kind: str, source: Path, mailbox: dict[str, str], runner_base_url: str,
    token: str, profile_summary: str,
) -> RunDir:
    """``data/runner-runs/<run_id>/``: extension copy + generated config, mode 700."""
    if not _RUN_ID.fullmatch(run_id or ""):
        raise _launch_error("运行编号无效")
    base = str(runner_base_url or "").rstrip("/")
    root = RUNS_ROOT / run_id
    try:
        RUNS_ROOT.mkdir(parents=True, exist_ok=True)
        _chmod(RUNS_ROOT, 0o700)
        root.mkdir(mode=0o700)
        _chmod(root, 0o700)
        extension = root / EXTENSION_DIR
        # The local private-config.mjs holds the operator's own settings; the run gets its own.
        shutil.copytree(Path(source), extension, ignore=shutil.ignore_patterns(PRIVATE_CONFIG, ".DS_Store", "*.zip"))
        config = extension / PRIVATE_CONFIG
        config.write_text(_private_config(mailbox=mailbox, runner_base_url=base, run_id=run_id, token=token),
                          encoding="utf-8", newline="\n")
        _chmod(config, 0o600)
        _rewrite_manifest(extension / "manifest.json", kind=kind, runner_base_url=base,
                          mailbox_base_url=str(mailbox.get("base_url") or ""))
        (extension / RUNNER_CONFIG).write_text(json.dumps({
            "schema": 1, "runId": run_id, "kind": kind,
            "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "profileSummary": profile_summary,
        }, ensure_ascii=False) + "\n", encoding="utf-8")
        return RunDir(root=root, extension=extension, log=root / LOG_NAME)
    except (OSError, ValueError) as exc:
        remove_tree(root)
        raise _launch_error(f"运行目录生成失败（{type(exc).__name__}）") from None


def _chmod(path: Path, mode: int) -> None:
    if os.name != "nt":
        os.chmod(path, mode)


def remove_tree(path: Path | None) -> None:
    """Best-effort delete; a failure is logged, never raised."""
    if path is None:
        return
    path = Path(path)

    def _writable(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    for attempt in range(5):
        if not path.exists():
            return
        try:
            shutil.rmtree(path, onerror=_writable)
        except OSError:
            pass
        if not path.exists():
            return
        # Windows keeps browser files locked for a moment after the process exits.
        time.sleep(0.5 * (attempt + 1))
    logger.warning("runner directory could not be removed: %s", path.name)


@dataclass
class ProxyHandle:
    """Credential-free proxy for ``chromix_args``; owns the SOCKS5 auth bridge when needed."""

    server: str
    bridge: SocksAuthBridge | None = None

    def close(self) -> None:
        bridge, self.bridge = self.bridge, None
        if bridge is not None:
            bridge.close()


def open_proxy(proxy_url: str) -> ProxyHandle:
    """SOCKS5 with credentials -> local bridge; plain http(s)/socks5 passes through.

    Authenticated http(s) proxies were already rejected by ``check_runner_proxy``.
    """
    try:
        normalized = normalize_proxy_url(proxy_url)
    except ValueError:
        normalized = None
    if not normalized:
        raise RunnerEnvironmentError("proxy_missing", "母号尚未配置有效代理")
    parsed = urlsplit(normalized)
    try:
        port = parsed.port
    except ValueError:
        port = None
    if not parsed.hostname or not port:
        raise _launch_error("浏览器代理格式无效")
    if parsed.username or parsed.password:
        if parsed.scheme not in {"socks5", "socks5h"}:
            raise RunnerEnvironmentError("proxy_auth_unsupported", "带账号密码的 HTTP 代理无法用于扩展运行器")
        try:
            bridge = SocksAuthBridge(parsed.hostname, port, unquote(parsed.username or ""), unquote(parsed.password or ""))
        except (OSError, ValueError):
            raise _launch_error("SOCKS5 认证桥启动失败") from None
        return ProxyHandle(server=f"socks5://127.0.0.1:{bridge.port}", bridge=bridge)
    host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
    return ProxyHandle(server=f"{parsed.scheme}://{host}:{port}")


def _x_alive(number: str) -> bool:
    path = f"/tmp/.X11-unix/X{number}"
    if not os.path.exists(path):
        return False
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.settimeout(0.4)
        conn.connect(path)
        return True
    except OSError:
        return False
    finally:
        conn.close()


def ensure_display() -> dict[str, str]:
    """Headed Chromix needs an X display; start Xvfb 1920x1080 on :99 when none is alive.

    Blocking (up to ~4 s); call via ``asyncio.to_thread``. Returns env additions.
    """
    if os.name == "nt":
        return {}
    display = os.environ.get("DISPLAY") or DEFAULT_DISPLAY
    match = re.fullmatch(r":(\d+)(?:\.\d+)?", display)
    if match is None:
        return {"DISPLAY": display}  # remote / forwarded display: trust the operator
    number = match.group(1)
    if _x_alive(number):
        return {"DISPLAY": display}
    if not shutil.which("Xvfb"):
        raise _launch_error("服务器没有可用的 X 显示（缺少 Xvfb），无法启动有界面浏览器")
    for stale in (f"/tmp/.X11-unix/X{number}", f"/tmp/.X{number}-lock"):
        try:
            os.remove(stale)
        except (FileNotFoundError, IsADirectoryError):
            pass
        except OSError:
            pass
    Path("/tmp/.X11-unix").mkdir(parents=True, exist_ok=True)
    with open("/tmp/xvfb.log", "ab", buffering=0) as log:
        subprocess.Popen(
            ["Xvfb", f":{number}", "-screen", "0", XVFB_SCREEN, "-ac", "+extension", "GLX", "+render", "-noreset"],
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
    for _ in range(40):
        if _x_alive(number):
            os.environ.setdefault("DISPLAY", f":{number}")
            return {"DISPLAY": f":{number}"}
        time.sleep(0.1)
    raise _launch_error("Xvfb 未能启动")


def clear_singleton_locks(user_data_dir: Path | str) -> None:
    """Drop stale profile locks; the caller holds the global browser slot, so no other browser uses them."""
    for name in _SINGLETON_FILES:
        try:
            (Path(user_data_dir) / name).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


def assert_clean_command(command: list[str]) -> None:
    """Last guard before exec: no automation / debugging / headless switches, no Playwright binary."""
    if not command or "playwright" in str(command[0]).lower():
        raise _launch_error("运行器不能使用 Playwright 自带的浏览器")
    if any(str(arg).lower().startswith(FORBIDDEN_FLAGS) for arg in command[1:]):
        raise _launch_error("运行器启动参数包含自动化开关")


def _child_env(timezone: str, extra: dict[str, str]) -> dict[str, str]:
    if os.name == "nt":
        env = {key: value for key, value in os.environ.items()
               if key.lower() not in _PROXY_ENV and not _SECRET_ENV.search(key)}
    else:
        env = {key: os.environ[key] for key in _POSIX_ENV if key in os.environ}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env.update(extra)
    if os.name != "nt":
        # Pages follow --fingerprint-timezone; TZ keeps the process clock (logs, ICU default) on the same zone.
        env["TZ"] = timezone
    return env


def launch(
    executable: str, args: list[str], *, run_dir: RunDir, timezone: str, extra_env: dict[str, str] | None = None,
) -> subprocess.Popen:
    """Start Chromix in its own process group; stdout/stderr go to the run's browser.log."""
    command = [str(executable), *args, "about:blank"]
    assert_clean_command(command)
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stderr": subprocess.STDOUT,
        "cwd": str(run_dir.root),
        "env": _child_env(timezone, extra_env or {}),
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        with open(run_dir.log, "ab", buffering=0) as log:
            process = subprocess.Popen(command, stdout=log, **kwargs)
    except OSError as exc:
        raise _launch_error(f"Chromix 启动失败（{type(exc).__name__}）") from None
    logger.info("extension runner browser started pid=%s", process.pid)
    return process


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    try:
        os.killpg(process.pid, sig)
    except (ProcessLookupError, PermissionError):
        pass
    except OSError:
        pass


def terminate(process: subprocess.Popen | None, *, grace: float = 5.0) -> None:
    """TERM the whole process group, KILL after ``grace`` seconds. Blocking."""
    if process is None:
        return
    if os.name == "nt":
        if process.poll() is None:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T"], capture_output=True, timeout=15, check=False)
        try:
            process.wait(grace)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True, timeout=15, check=False)
            try:
                process.wait(grace)
            except subprocess.TimeoutExpired:
                logger.warning("extension runner browser did not exit pid=%s", process.pid)
        return
    if process.poll() is None:
        _signal_group(process, signal.SIGTERM)
        try:
            process.wait(grace)
        except subprocess.TimeoutExpired:
            pass
    # Renderers and helpers share the group even after the leader exits.
    _signal_group(process, signal.SIGKILL)
    try:
        process.wait(grace)
    except subprocess.TimeoutExpired:
        logger.warning("extension runner browser did not exit pid=%s", process.pid)


def kill_now(process: subprocess.Popen | None) -> None:
    """Non-blocking last resort used when shutdown itself is cancelled."""
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        subprocess.Popen(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        _signal_group(process, signal.SIGKILL)


def sanitize_log_line(line: str, limit: int = 300) -> str:
    text = _URL_QUERY.sub(r"\1", str(line or "").rstrip())
    return _EMAIL.sub("***", text)[:limit]


def log_tail(path: Path | None, lines: int = LOG_TAIL_LINES) -> list[str]:
    """Last lines of browser.log without URL queries or addresses."""
    if path is None:
        return []
    try:
        with open(path, "rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - 64 * 1024))
            raw = stream.read()
    except OSError:
        return []
    text = raw.decode("utf-8", errors="replace").splitlines()
    return [sanitize_log_line(line) for line in text[-lines:] if line.strip()]
