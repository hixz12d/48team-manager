"""Extension runner: Chromix subprocess + the real signup extension, driven over HTTP.

Protocol: ``docs/contracts/extension-runner.md`` (single source of truth).
Part-0 fixes the public signatures, data classes and error codes; part-2 fills in
``signup_and_authorize`` / ``run_selfcheck`` and the route-facing helpers.
"""
from __future__ import annotations

import asyncio
import base64
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import hashlib
import hmac
import ipaddress
import json
import logging
from pathlib import Path
import re
import subprocess
import time
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.core.config import BASE_DIR, load_settings
from app.core.proxy import normalize_proxy_url
from app.integrations.openai.browser import runner_process
from app.integrations.openai.browser.runner_profile import (
    RunnerEnvironmentError, chromix_args, create_runner_profile, exit_ip_hash, load_runner_profile,
    profile_summary, runner_profile_dir, runner_profiles_root,
)
from app.integrations.proxy.geo import GeoLookupError, lookup_exit

logger = logging.getLogger(__name__)

# ---- Error codes shared by part-2 (runner) and part-3 (rotation) ----
RUNNER_NOT_CONFIGURED = "runner_not_configured"
RUNNER_EXECUTABLE_MISSING = "runner_executable_missing"
PROXY_AUTH_UNSUPPORTED = "proxy_auth_unsupported"
RUNNER_GEO_UNKNOWN = "runner_geo_unknown"
RUNNER_PROFILE_INVALID = "runner_profile_invalid"
RUNNER_LAUNCH_FAILED = "runner_launch_failed"
RUNNER_EXITED = "runner_exited"
RUNNER_HEARTBEAT_LOST = "runner_heartbeat_lost"
RUNNER_TIMEOUT = "runner_timeout"
NOT_JOINED = "not_joined"
MEMBERSHIP_MISMATCH = "membership_mismatch"
OAUTH_EXCHANGE_FAILED = "oauth_exchange_failed"
TOKEN_IDENTITY_MISMATCH = "token_identity_mismatch"
RUNNER_PAUSED_PREFIX = "runner_paused_"

# Pause reasons whose codes must match rotation's MANUAL_MARKERS ("phone", "captcha", ...).
PAUSE_ERROR_CODES = {
    "phone": "phone_verification_required",
    "captcha": "captcha_required",
}

# Timing contract (seconds); see the protocol document.
HEARTBEAT_INTERVAL = 30
HEARTBEAT_LOST_AFTER = 120
AUTHORIZE_POLL_INTERVAL = 5
SELFCHECK_TIMEOUT = 240

# Request size limits for /api/ext/runner/*. An event may carry the final
# diagnosticReport (up to 200 events), so it gets the same 64KB as JSON probes.
EVENT_MAX_BYTES = 64 * 1024
PROBE_JSON_MAX_BYTES = 64 * 1024
PROBE_SCREENSHOT_MAX_BYTES = 4 * 1024 * 1024
RUN_ID_PATTERN = r"^[a-f0-9]{32}$"          # secrets.token_hex(16)
PROBE_NAME_PATTERN = r"^[a-z0-9_-]{1,40}$"

# /callback rejection codes (the plugin does not retry these).
CALLBACK_INVALID = "callback_invalid"
CALLBACK_ALREADY_RECEIVED = "callback_already_received"
NOT_AUTHORIZING = "not_authorizing"

SELFCHECK_PROBE_URLS = (
    {"name": "browserscan", "url": "https://www.browserscan.net/"},
    {"name": "creepjs", "url": "https://abrahamjuliot.github.io/creepjs/"},
    {"name": "browserleaks_webrtc", "url": "https://browserleaks.com/webrtc"},
)

# A non-final pause that neither resumes nor becomes final within this time ends the run.
NONFINAL_PAUSE_LIMIT = 180

REQUIRED_EXTENSION_FILES = ("manifest.json", "background.js", "content.js", "shared.mjs", "cloudflare.mjs", "runner.mjs")

# Rotation (part-3): the new account already joined but has no credentials; a person must authorize.
RUNNER_OAUTH_REQUIRED = "runner_oauth_required"
# Cloudflare mailbox settings missing or not the origin the extension's cloudflare.mjs accepts.
RUNNER_MAILBOX_INVALID = "runner_mailbox_invalid"

OnStage = Callable[[str, str], Awaitable[None]]


def paused_error_code(pause_reason: str) -> str:
    """Error code for a plugin pause; unknown reasons keep the ``runner_paused_`` prefix."""
    reason = str(pause_reason or "other").strip().lower() or "other"
    return PAUSE_ERROR_CODES.get(reason, RUNNER_PAUSED_PREFIX + reason)


@dataclass
class RunnerOutcome:
    ok: bool
    error_code: str = ""
    error: str = ""
    joined: bool = False          # official member list confirmed the new account joined
    authorized: bool = False      # tokens exchanged and stored
    diagnostics: dict | None = None


def _error(code: str, message: str) -> RunnerEnvironmentError:
    """A ``BrowserEnvironmentError`` subclass: existing handlers read ``exc.error_code``."""
    return RunnerEnvironmentError(code, message)


def runner_enabled() -> bool:
    return load_settings().rotation_signup_runner == "extension"


def validate_runner_configuration() -> None:
    """Executable exists and extension sources are complete; no writes, no network."""
    settings = load_settings()
    executable = str(settings.runner_browser_executable or "").strip()
    if not executable:
        raise _error(RUNNER_NOT_CONFIGURED, "未配置 RUNNER_BROWSER_EXECUTABLE（Chromix 路径）")
    path = Path(executable)
    if not path.is_file() or path.suffix.lower() in {".zip", ".cmd", ".bat"}:
        raise _error(RUNNER_EXECUTABLE_MISSING, "RUNNER_BROWSER_EXECUTABLE 指向的 Chromix 文件不存在")
    extension = settings.runner_extension_path
    missing = [name for name in REQUIRED_EXTENSION_FILES if not (extension / name).is_file()]
    if missing:
        raise _error(RUNNER_NOT_CONFIGURED, "插件目录不完整，缺少：" + "、".join(missing))
    base = urlsplit(settings.runner_local_base_url)
    if base.scheme != "http" or base.hostname not in {"127.0.0.1", "localhost"} or base.path not in {"", "/"}:
        raise _error(RUNNER_NOT_CONFIGURED, "RUNNER_LOCAL_BASE_URL 必须是本机 http 地址，如 http://127.0.0.1:8008")
    if settings.runner_timeout_seconds < 300:
        raise _error(RUNNER_NOT_CONFIGURED, "RUNNER_TIMEOUT_SECONDS 不能小于 300")


async def check_runner_proxy(proxy_url: str) -> None:
    """Reject proxies a Chromix subprocess cannot use; SOCKS5 credentials go through the bridge."""
    try:
        normalized = normalize_proxy_url(proxy_url)
    except ValueError:
        normalized = None
    if not normalized:
        raise _error("proxy_missing", "母号尚未配置有效代理")
    parsed = urlsplit(normalized)
    if parsed.scheme in {"http", "https"} and (parsed.username or parsed.password):
        raise _error(PROXY_AUTH_UNSUPPORTED, "带账号密码的 HTTP 代理无法用于扩展运行器，请改用 SOCKS5 或无认证代理")


# ---- In-process run registry (lost on restart; rotation then marks the job manual_required) ----

EVENT_KEEP = 50
STOP_GRACE = 3.0
TERMINATE_GRACE = 5.0
OAUTH_DONE_CALLBACK_GRACE = 30
SELFCHECK_KEEP = 10
SELFCHECK_ROOT = BASE_DIR / "data" / "selfcheck"
SELFCHECK_PROFILES_ROOT = BASE_DIR / "data" / "chrome-profiles" / "runner-selfcheck"
_JOB_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_STATUSES = {"running", "paused", "stopped", "done"}
_PHASES = {"signup", "oauth", "selfcheck"}
_CODEX_RESULTS = {"unknown", "no_phone", "phone_required", "failed"}
_REASON = re.compile(r"^[a-z_]{1,40}$")
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.I)
_EMAIL = re.compile(r"[^\s@\"'<>]+@[^\s@\"'<>]+")
_CODE = re.compile(r"(?<!\d)\d{6}(?!\d)")
# Plugin stage -> existing browser stage names (HME "signup started" marking, progress plan).
_STAGE_PROGRESS = {"otp": "email_otp", "profile": "about_you"}
# Phone pages appear in both phases (signup and Codex authorization).
_PHONE_STAGE_PROGRESS = {"phone": "add_phone", "phone_otp": "sms_otp"}
_EXIT_KEYS = ("ip", "country", "region", "city", "timezone", "org")


@dataclass
class RunState:
    run_id: str
    token_hash: str
    kind: str
    job: dict[str, Any]
    started_at: float = field(default_factory=time.monotonic)
    last_event_at: float = field(default_factory=time.monotonic)
    status: str = "starting"
    phase: str = ""
    stage: str = ""
    pause_reason: str = ""
    pause_final: bool = False
    paused_since: float | None = None
    codex_result: str = "unknown"
    max_seq: int = 0
    event_count: int = 0
    events: deque = field(default_factory=lambda: deque(maxlen=EVENT_KEEP))
    command: str = "none"          # none / authorize / stop
    authorize_url: str = ""
    oauth_seen: bool = False
    callback_url: str = ""
    probes: dict[str, Any] = field(default_factory=dict)
    screenshots: dict[str, str] = field(default_factory=dict)
    screenshot_dir: Path | None = None
    plugin_diagnostics: dict[str, Any] | None = None
    # Phone relay (docs/contracts/phone-relay.md): only signup runs started with the pool enabled.
    phone_pool: bool = False
    phone_lease_key: str = ""
    phone_account_id: int | None = None
    phone_proxy_url: str = ""
    closed: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)


_RUNS: dict[str, RunState] = {}


def _token_hash(token: str) -> str:
    return hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()


def _lookup(run_id: str, token: str) -> RunState | None:
    if not isinstance(run_id, str) or not re.fullmatch(RUN_ID_PATTERN, run_id):
        return None
    state = _RUNS.get(run_id)
    if state is None or state.closed or not isinstance(token, str) or not token:
        return None
    if not hmac.compare_digest(_token_hash(token), state.token_hash):
        return None
    return state


def _clean_text(value: Any, limit: int = 200) -> str:
    text = _URL.sub("[链接]", str(value or ""))
    text = _EMAIL.sub("***", text)
    return _CODE.sub("******", text).replace("\n", " ")[:limit]


def _command_body(state: RunState) -> dict[str, Any]:
    if state.command == "stop":
        return {"ok": True, "command": "stop"}
    if state.command == "authorize" and state.authorize_url and not state.oauth_seen:
        return {"ok": True, "command": "authorize", "authorizeUrl": state.authorize_url}
    return {"ok": True, "command": "none"}


def _valid_callback(url: str) -> bool:
    """Same rule as the plugin's ``shared.mjs`` ``oauthCallback``."""
    try:
        parsed = urlsplit(str(url or ""))
        port = parsed.port
    except ValueError:
        return False
    if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1"} or port != 1455:
        return False
    if parsed.path != "/auth/callback" or parsed.username or parsed.password:
        return False
    from urllib.parse import parse_qs
    query = parse_qs(parsed.query, keep_blank_values=True)
    return "state" in query and ("code" in query or "error" in query)


def runner_authorized(run_id: str, token: str) -> bool:
    """Token check before the route reads a body, so a wrong token is 404 rather than 413/422."""
    return _lookup(run_id, token) is not None


def runner_job(run_id: str, token: str) -> dict[str, Any] | None:
    """Body of ``GET /api/ext/runner/{run_id}/job``."""
    state = _lookup(run_id, token)
    if state is None:
        return None
    state.last_event_at = time.monotonic()
    return json.loads(json.dumps(state.job))


def runner_event(run_id: str, token: str, event: dict[str, Any]) -> dict[str, Any] | None:
    """Record one plugin event; returns ``{"ok": True, "command": ..., "authorizeUrl"?: ...}``."""
    state = _lookup(run_id, token)
    if state is None:
        return None
    now = time.monotonic()
    state.last_event_at = now
    try:
        seq = int(event.get("seq") or 0)
    except (TypeError, ValueError):
        seq = 0
    if seq <= state.max_seq:
        return _command_body(state)
    status = str(event.get("status") or "")
    phase = str(event.get("phase") or "")
    if status not in _STATUSES or phase not in _PHASES:
        return _command_body(state)
    state.max_seq = seq
    state.event_count += 1
    reason = str(event.get("pauseReason") or "").strip().lower() if status == "paused" else ""
    if reason and not _REASON.fullmatch(reason):
        reason = "other"
    if status == "paused":
        if state.status != "paused" or state.pause_reason != reason:
            state.paused_since = now
    else:
        state.paused_since = None
    state.status, state.phase = status, phase
    state.stage = str(event.get("stage") or "unknown")[:40]
    state.pause_reason = reason
    state.pause_final = bool(event.get("pauseFinal")) if status == "paused" else False
    codex = str(event.get("codexResult") or "unknown")
    state.codex_result = codex if codex in _CODEX_RESULTS else "unknown"
    if phase == "oauth":
        state.oauth_seen = True
    diagnostics = event.get("diagnostics")
    if isinstance(diagnostics, dict):
        state.plugin_diagnostics = diagnostics
    state.events.append({
        "t": round(now - state.started_at, 1), "seq": seq, "status": status, "phase": phase,
        "stage": state.stage, "pauseReason": reason or None, "pauseFinal": state.pause_final,
        "codexResult": state.codex_result, "message": _clean_text(event.get("message")),
    })
    state.wake.set()
    return _command_body(state)


def runner_callback(run_id: str, token: str, callback_url: str) -> dict[str, Any] | None:
    """Accept the OAuth loopback callback; returns ``{"ok": True}`` or ``{"ok": False, "error_code": ...}``."""
    state = _lookup(run_id, token)
    if state is None:
        return None
    state.last_event_at = time.monotonic()
    url = str(callback_url or "").strip()
    if state.kind != "signup" or not state.authorize_url:
        return {"ok": False, "error_code": NOT_AUTHORIZING}
    if state.callback_url:
        if hmac.compare_digest(state.callback_url.encode("utf-8"), url.encode("utf-8")):
            return {"ok": True}
        return {"ok": False, "error_code": CALLBACK_ALREADY_RECEIVED}
    if len(url) > 4096 or not _valid_callback(url):
        return {"ok": False, "error_code": CALLBACK_INVALID}
    state.callback_url = url
    state.wake.set()
    return {"ok": True}


def _store_screenshot(state: RunState, name: str, data: Any) -> None:
    if state.screenshot_dir is None or not isinstance(data, str):
        return
    allowed = {item.get("name") for item in state.job.get("probeUrls") or []}
    prefix = "data:image/png;base64,"
    if name not in allowed or not data.startswith(prefix) or len(data) > PROBE_SCREENSHOT_MAX_BYTES:
        return
    try:
        raw = base64.b64decode(data[len(prefix):], validate=True)
    except ValueError:
        return
    if not raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return
    target = state.screenshot_dir / f"{name}.png"
    temporary = target.with_suffix(".tmp")
    try:
        state.screenshot_dir.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(raw)
        temporary.replace(target)
    except OSError:
        logger.warning("selfcheck screenshot could not be saved: %s", name)
        return
    state.screenshots[name] = target.relative_to(BASE_DIR).as_posix()


def runner_probe(run_id: str, token: str, *, name: str, kind: str, data: Any) -> dict[str, Any] | None:
    """Store one self-check probe (signals / exit / screenshot); returns ``{"ok": True}``."""
    state = _lookup(run_id, token)
    if state is None:
        return None
    state.last_event_at = time.monotonic()
    if state.kind != "selfcheck" or not re.fullmatch(PROBE_NAME_PATTERN, str(name or "")):
        return {"ok": True}
    if kind == "screenshot":
        _store_screenshot(state, name, data)
    elif kind in {"signals", "exit"} and isinstance(data, dict):
        if len(json.dumps(data, ensure_ascii=False).encode("utf-8")) <= PROBE_JSON_MAX_BYTES:
            state.probes[kind] = data
    state.wake.set()
    return {"ok": True}


def runner_phone_context(run_id: str, token: str) -> dict[str, Any] | None:
    """Relay context for ``POST /api/ext/runner/{run_id}/phone``; ``None`` means 404."""
    state = _lookup(run_id, token)
    if state is None:
        return None
    state.last_event_at = time.monotonic()  # SMS polling counts as a heartbeat
    if state.kind != "signup" or not state.phone_pool:
        return {"enabled": False}
    return {
        "enabled": True,
        "lease_key": state.phone_lease_key,
        "account_id": state.phone_account_id,
        "proxy_url": state.phone_proxy_url,
        "phase": state.phase if state.phase in {"signup", "oauth"} else "signup",
    }


# ---- Shared launch / wait / shutdown ----

class _RunFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _Progress:
    """Task progress: ``on_stage`` when the caller gave one, otherwise the job's own steps."""

    def __init__(self, db, job_id: str, on_stage: OnStage | None) -> None:
        self.db = db
        self.job_id = job_id
        self.on_stage = on_stage
        self.last_heartbeat = time.monotonic()
        self.sent: set[str] = set()

    async def stage(self, stage: str, message: str = "") -> None:
        if self.on_stage is not None:
            await self.on_stage(stage, message)
        elif self.job_id:
            from app.application.invitation_flow import browser_progress
            await browser_progress(self.db, self.job_id, stage, message)
        if stage == "heartbeat":
            self.last_heartbeat = time.monotonic()

    async def once(self, stage: str, message: str = "") -> None:
        if stage not in self.sent:
            self.sent.add(stage)
            await self.stage(stage, message)

    async def heartbeat(self, force: bool = False) -> None:
        if force or time.monotonic() - self.last_heartbeat >= HEARTBEAT_INTERVAL:
            await self.stage("heartbeat", "")

    async def note(self, stage: str, message: str, *keep) -> None:
        """Verbatim step text (``browser_progress`` would replace it with the stage code)."""
        if not self.job_id:
            return
        try:
            from app.application.operations import operation_store
            op = await operation_store.get_by_public_id(self.db, self.job_id)
            if op is not None:
                await operation_store.note(self.db, op, stage, message[:300])
                await self.db.commit()
        except Exception:  # noqa: BLE001 - diagnostics must never break cleanup
            await _recover(self.db, *keep)
            logger.warning("runner diagnostics note failed", exc_info=True)


async def _rollback(db) -> None:
    try:
        await db.rollback()
    except Exception:  # noqa: BLE001
        pass


async def _recover(db, *objects) -> None:
    """Roll back, then reload the caller's ORM objects: after a rollback they are expired and
    reading them in async code would fail (the rotation reads ``account`` right after we return)."""
    await _rollback(db)
    for obj in objects:
        if obj is None:
            continue
        try:
            await db.refresh(obj)
        except Exception:  # noqa: BLE001
            logger.warning("runner could not reload %s after rollback", type(obj).__name__)


def _geo_error(exc: Exception | None = None) -> RunnerEnvironmentError:
    return RunnerEnvironmentError(RUNNER_GEO_UNKNOWN, "未能经代理查到出口 IP 所在时区，未创建浏览器档案")


_MAIL_ORIGIN = re.compile(r"""export\s+const\s+MAIL_ORIGIN\s*=\s*['"]([^'"]+)['"]""")


def _extension_mail_origin() -> str:
    """The only mailbox origin the extension's ``cloudflare.mjs`` accepts (read from the source)."""
    try:
        text = (load_settings().runner_extension_path / "cloudflare.mjs").read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _MAIL_ORIGIN.search(text)
    return match.group(1).rstrip("/") if match else ""


async def check_runner_mailbox(db) -> dict[str, str]:
    """Cloudflare mailbox settings usable by the extension; raises ``runner_mailbox_invalid``.

    Returns the ``load_cf_config`` dict with ``base_url`` reduced to the bare origin.
    """
    from app.application.reauth import load_cf_config

    config = await load_cf_config(db)
    if not all(config.get(key) for key in ("base_url", "address", "admin_password")):
        raise RunnerEnvironmentError(RUNNER_MAILBOX_INVALID, "未配置完整的 Cloudflare 邮箱（地址、收件邮箱、管理密码），插件无法读取验证码")
    parsed = urlsplit(config["base_url"])
    expected = _extension_mail_origin()
    origin = runner_process.origin_of(config["base_url"])
    if (not expected or origin != expected or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        raise RunnerEnvironmentError(
            RUNNER_MAILBOX_INVALID,
            f"Cloudflare 邮箱地址必须是插件内置的 {expected or '（插件未声明）'}，当前设置不一致，未启动浏览器",
        )
    return {**config, "base_url": origin}


async def _ensure_profile(directory: Path, proxy_url: str, *, platform: str, gpu_mode: str) -> tuple[dict[str, Any], str]:
    """Existing profile first; otherwise look up the exit through the proxy and create it.

    For an existing profile the exit is re-checked (best effort) and only reported:
    the persona is never changed or re-randomised.
    """
    profile = await asyncio.to_thread(load_runner_profile, directory)
    if profile is None:
        try:
            geo = await lookup_exit(proxy_url)
        except GeoLookupError as exc:
            raise _geo_error(exc) from None
        profile = await asyncio.to_thread(create_runner_profile, directory, geo=geo, platform=platform, gpu_mode=gpu_mode)
        return profile, ""
    try:
        geo = await lookup_exit(proxy_url)
    except GeoLookupError:
        return profile, "当前出口未能复核"
    if geo["country"] != profile["exit_country"]:
        return profile, f"注意：当前出口国家 {geo['country']} 与档案 {profile['exit_country']} 不符，时区仍按档案"
    if geo["timezone"] != profile["timezone"]:
        return profile, f"注意：当前出口时区 {geo['timezone']} 与档案不同，仍按档案"
    if exit_ip_hash(geo["ip"]) != profile["exit_ip_hash"]:
        return profile, "当前出口 IP 已变化（同国家、同时区）"
    return profile, ""


@dataclass
class _Launched:
    state: RunState
    token: str
    proxy_url: str
    run_dir: runner_process.RunDir | None = None
    proxy: runner_process.ProxyHandle | None = None
    process: subprocess.Popen | None = None


async def _launch(
    launched: _Launched, *, mailbox: dict[str, str], profile: dict[str, Any], user_data_dir: Path, summary: str,
) -> None:
    """Run directory -> proxy (bridge) -> display -> Chromix child process. No Playwright, no CDP."""
    settings = load_settings()
    state = launched.state
    launched.run_dir = await asyncio.to_thread(
        runner_process.prepare_run_dir, state.run_id, kind=state.kind, source=settings.runner_extension_path,
        mailbox=mailbox, runner_base_url=settings.runner_local_base_url, token=launched.token,
        profile_summary=summary,
    )
    launched.token = ""  # only the hash stays in memory
    launched.proxy = runner_process.open_proxy(launched.proxy_url)
    args = chromix_args(profile, user_data_dir=user_data_dir, extension_dir=launched.run_dir.extension,
                        proxy_server=launched.proxy.server)
    display = await asyncio.to_thread(runner_process.ensure_display)
    await asyncio.to_thread(Path(user_data_dir).mkdir, parents=True, exist_ok=True)
    runner_process.clear_singleton_locks(user_data_dir)
    launched.process = runner_process.launch(
        settings.runner_browser_executable.strip(), args, run_dir=launched.run_dir,
        timezone=profile["timezone"], extra_env=display,
    )
    now = time.monotonic()
    state.started_at = state.last_event_at = now
    state.status = "launched"


def _check_run(state: RunState, process: subprocess.Popen | None, *, deadline: float, timeout_code: str = RUNNER_TIMEOUT) -> None:
    """Raise the protocol failure for exits, silence, timeouts and plugin pauses/stops."""
    now = time.monotonic()
    if state.status == "paused":
        waited = now - (state.paused_since or now)
        if state.pause_final or waited >= NONFINAL_PAUSE_LIMIT:
            reason = state.pause_reason or "other"
            raise _RunFailure(paused_error_code(reason), f"插件在{_phase_label(state.phase)}阶段暂停（{reason}），需要人工处理")
    if state.status == "stopped" and state.command != "stop":
        raise _RunFailure(RUNNER_EXITED, "插件已自行停止")
    if process is not None and process.poll() is not None:
        raise _RunFailure(RUNNER_EXITED, f"浏览器进程已退出（代码 {process.returncode}）")
    if now - state.last_event_at > HEARTBEAT_LOST_AFTER:
        raise _RunFailure(RUNNER_HEARTBEAT_LOST, f"插件超过 {HEARTBEAT_LOST_AFTER} 秒没有回报")
    if now > deadline:
        raise _RunFailure(timeout_code, "运行超时，已关闭浏览器")


def _phase_label(phase: str) -> str:
    return {"signup": "注册", "oauth": "授权", "selfcheck": "自检"}.get(phase, "启动")


async def _wait_wake(state: RunState, seconds: float = AUTHORIZE_POLL_INTERVAL) -> None:
    try:
        await asyncio.wait_for(state.wake.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass
    state.wake.clear()


async def _shutdown(launched: _Launched) -> list[str]:
    """stop -> 3 s -> TERM -> 5 s -> KILL (process group), close bridge, unregister, delete run dir.

    Returns the sanitised tail of ``browser.log`` (read before the directory is deleted).
    """
    state = launched.state
    process = launched.process
    tail: list[str] = []
    try:
        state.command = "stop"
        state.wake.set()
        if process is not None and process.poll() is None and state.status != "stopped":
            end = time.monotonic() + STOP_GRACE
            while time.monotonic() < end and process.poll() is None and state.status != "stopped":
                await asyncio.sleep(0.25)
        state.closed = True
        _RUNS.pop(state.run_id, None)
        if process is not None:
            await asyncio.to_thread(runner_process.terminate, process, grace=TERMINATE_GRACE)
    except BaseException:
        runner_process.kill_now(process)
        raise
    finally:
        state.closed = True
        _RUNS.pop(state.run_id, None)
        if launched.proxy is not None:
            launched.proxy.close()
        if launched.run_dir is not None:
            tail = runner_process.log_tail(launched.run_dir.log)
            await asyncio.to_thread(runner_process.remove_tree, launched.run_dir.root)
    return tail


def _plugin_diagnostics(state: RunState) -> dict[str, Any] | None:
    data = state.plugin_diagnostics
    if not isinstance(data, dict):
        return None
    text = json.dumps(data, ensure_ascii=False)
    if len(text.encode("utf-8")) > EVENT_MAX_BYTES or _EMAIL.search(text) or _URL.search(text):
        # Reports are sanitised by the plugin; anything that still looks like an address or URL is dropped.
        return {"dropped": True, "reason": "unexpected_content"}
    return data


def _diagnostics(state: RunState, summary: str, browser_log: list[str]) -> dict[str, Any]:
    return {
        "run_kind": state.kind,
        "profile_summary": summary,
        "duration_seconds": round(time.monotonic() - state.started_at, 1),
        "last": {"status": state.status, "phase": state.phase, "stage": state.stage,
                 "pauseReason": state.pause_reason or None, "codexResult": state.codex_result},
        "event_count": state.event_count,
        "events": list(state.events),
        "plugin": _plugin_diagnostics(state),
        "browser_log": browser_log,
    }


def _diagnostics_line(diagnostics: dict[str, Any], error_code: str) -> str:
    last = diagnostics.get("last") or {}
    plugin = diagnostics.get("plugin") or {}
    version = plugin.get("version") if isinstance(plugin, dict) else None
    return (f"运行器结束：{error_code or '成功'}；插件状态 {last.get('status')}/{last.get('phase') or '-'}"
            f"/{last.get('stage') or '-'}，暂停原因 {last.get('pauseReason') or '无'}，"
            f"事件 {diagnostics.get('event_count', 0)} 条，插件版本 {version or '未知'}，"
            f"用时 {diagnostics.get('duration_seconds')} 秒")


def _new_run(kind: str, job: dict[str, Any], proxy_url: str) -> _Launched:
    run_id = runner_process.new_run_id()
    while run_id in _RUNS:
        run_id = runner_process.new_run_id()
    token = runner_process.new_token()
    state = RunState(run_id=run_id, token_hash=_token_hash(token), kind=kind, job=job)
    return _Launched(state=state, token=token, proxy_url=proxy_url)


async def _release_phones(db, state: RunState, *keep) -> None:
    """Free any number this run still holds; a failure only leaves it to the 180 s lease expiry."""
    from app.application.resources import phone_relay

    try:
        await phone_relay.release(db, phone_relay.RelayContext(
            lease_key=state.phone_lease_key, account_id=state.phone_account_id,
            proxy_url=state.phone_proxy_url, phase="signup",
        ))
    except Exception as exc:  # noqa: BLE001 - cleanup must not change the run outcome
        await _recover(db, *keep)
        logger.debug("runner phone release failed: %s", type(exc).__name__)


def _unexpected(exc: BaseException, launched: _Launched | None) -> tuple[str, str]:
    """Map an unexpected error to a protocol code; the message never carries exception text."""
    logger.warning("extension runner failed unexpectedly: %s", type(exc).__name__)
    if launched is None or launched.process is None:
        return RUNNER_LAUNCH_FAILED, f"运行器启动失败（{type(exc).__name__}）"
    if launched.state.callback_url:
        return OAUTH_EXCHANGE_FAILED, f"换票过程出错（{type(exc).__name__}）"
    return RUNNER_EXITED, f"运行器出错（{type(exc).__name__}）"


# ---- signup ----

async def _confirm_member(db, workspace, email: str, progress: _Progress, state: RunState, process) -> tuple[dict | None, str]:
    from app.application.onboard import OnboardService
    from app.domain.onboard import JOIN_CONFIRM_ATTEMPTS, JOIN_CONFIRM_INTERVAL

    service = OnboardService()
    last_error = ""
    for attempt in range(JOIN_CONFIRM_ATTEMPTS):
        if attempt:
            await asyncio.sleep(JOIN_CONFIRM_INTERVAL)
            await progress.heartbeat()
        if process is not None and process.poll() is not None:
            break
        try:
            member = await service._confirm_joined(db, workspace, email)
        except Exception as exc:  # noqa: BLE001 - official list read failed before any write; retry
            last_error = _clean_text(exc, 120)
            continue
        if member:
            return member, ""
    return None, last_error


async def _begin_authorize(db, *, account, workspace, proxy_url: str, job_id: str) -> tuple[str, str, Any]:
    """Same session setup as ``oauth_signup.run_invited_oauth_signup``; returns (ticket, url, stored)."""
    from app.application.oauth_sessions import oauth_session_store
    from app.integrations.openai import oauth_sessions
    from app.integrations.openai.chatgpt import chatgpt_client

    authorize = chatgpt_client.create_oauth_authorize_url(
        client_id=oauth_sessions.CLIENT_ID, redirect_uri=oauth_sessions.REDIRECT_URI, login_hint=account.email,
    )
    public = oauth_sessions.create_session(
        team_id=int(workspace.source_team_id or 0),
        email=account.email,
        authorize=authorize,
        proxy=account.proxy or proxy_url,
        proxy_source=account.proxy_source or "",
        sub2api_proxy_id=account.sub2api_proxy_id,
        proxy_instance_key=account.proxy_instance_key or "",
    )
    ticket = public["ticket"]
    oauth_sessions.mark_session(ticket, job_id=job_id or "")
    try:
        stored = await oauth_session_store.persist(
            db, oauth_sessions.get_session(ticket), purpose="account_reauth",
            account_id=account.id, workspace_id=workspace.id,
            credential_revision=int(account.credential_revision or 1),
        )
        await db.commit()
    except BaseException:
        oauth_sessions.pop_session(ticket)
        raise
    return ticket, authorize["authorize_url"], stored


async def _exchange(db, *, account, ticket: str, callback_url: str) -> None:
    """``oauth_signup.py`` exchange rules: begin_exchange, revision, code exchange, token email."""
    from app.application.oauth_sessions import oauth_session_store
    from app.application.tokens import auth_service
    from app.core.jwt import jwt_parser
    from app.domain.identity.ids import normalize_email
    from app.integrations.openai.chatgpt import chatgpt_client

    stored, callback = await oauth_session_store.begin_exchange(
        db, ticket, callback_url, account_id=account.id, purpose="account_reauth",
    )
    await db.refresh(account)
    if stored.credential_revision != int(account.credential_revision or 1):
        raise _RunFailure(OAUTH_EXCHANGE_FAILED, "注册期间凭据已变化，未写入新凭据")
    context = oauth_session_store.exchange_context(stored)
    exchanged = await chatgpt_client.exchange_oauth_code(
        code=callback["code"], client_id=context["client_id"],
        redirect_uri=context["redirect_uri"], code_verifier=context["code_verifier"],
        db_session=db, identifier=account.email,
    )
    if not exchanged.get("success") or not exchanged.get("access_token") or not exchanged.get("refresh_token"):
        raise _RunFailure(OAUTH_EXCHANGE_FAILED, "授权码换票失败")
    token_email = jwt_parser.extract_email(exchanged["access_token"])
    if not token_email or normalize_email(token_email) != normalize_email(account.email):
        raise _RunFailure(TOKEN_IDENTITY_MISMATCH, "授权得到的账号邮箱与本账号不符")
    await auth_service.apply_tokens(account, {**exchanged, "client_id": context["client_id"]})
    account.auth_state = "healthy"
    await db.commit()


async def _signup_job(db, account, workspace, profile_dir: Path, *, phone_pool: bool = False) -> dict[str, Any]:
    from app.application.member_handoff import _workspace_names
    from app.application.tokens import decrypt_secret, encrypt_secret
    from app.domain.onboard import random_password
    from app.integrations.openai.browser.signup import signup_profile
    from app.persistence.models.identity import Account

    password = decrypt_secret(account.password_encrypted)
    if not password:
        password = random_password()
        account.password_encrypted = encrypt_secret(password)
        await db.commit()
    try:
        profile = await asyncio.to_thread(signup_profile, profile_dir)
    except (OSError, ValueError, KeyError):
        raise RunnerEnvironmentError(RUNNER_PROFILE_INVALID, "注册资料文件损坏，请恢复后继续") from None
    owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
    names = _workspace_names(workspace, owner.email if owner else None)[:5]
    return {
        "kind": "signup",
        "email": str(account.email or "").strip().lower(),
        "password": password,
        "profile": {"name": profile["name"], "birthday": profile["birthday"]},
        "mode": "auto",
        "workspaceNames": names,
        "probeUrls": [],
        "phonePool": bool(phone_pool),
    }


async def signup_and_authorize(
    db, *, account, workspace, role: str, seat_intent: str,
    proxy_url: str, job_id: str, on_stage: OnStage | None = None,
    use_phone_pool: bool = False,
) -> RunnerOutcome:
    """Launch browser -> signup -> confirm join -> authorize -> exchange -> store tokens.

    Preconditions: the invitation was sent and confirmed; the caller holds the global
    browser slot. Always closes the browser and deletes the run directory before return.
    ``use_phone_pool`` (with a ``job_id``) lets the extension relay phone checks through
    the number pool; the lease key is the job's public id.
    """
    from app.application.identity import ensure_membership
    from app.application.oauth_sessions import OAuthSessionError, oauth_session_store
    from app.core.time import utcnow
    from app.domain.identity import LOCAL_PURPOSE_CHILD, MEMBERSHIP_STATE_JOINED
    from app.integrations.openai import oauth_sessions
    from app.integrations.openai.member_adapter import (
        existing_invite_seat_error, normalize_official_role, official_roles_equivalent,
        parse_invite_role, parse_invite_seat_intent,
    )

    settings = load_settings()
    progress = _Progress(db, job_id, on_stage)
    launched: _Launched | None = None
    summary = ""
    joined = False
    authorized = False
    ticket = ""
    stored = None
    error_code, error = "", ""
    diagnostics: dict[str, Any] | None = None
    try:
        try:
            validate_runner_configuration()
            await check_runner_proxy(proxy_url)
            mailbox = await check_runner_mailbox(db)
            try:
                requested_role = parse_invite_role(role)
                requested_seat = parse_invite_seat_intent(seat_intent)
            except ValueError:
                raise RunnerEnvironmentError(RUNNER_NOT_CONFIGURED, "角色或席位参数无效") from None
            profile_dir = runner_profile_dir(account.email)
            profile, exit_note = await _ensure_profile(
                profile_dir, proxy_url, platform=settings.runner_fingerprint_platform, gpu_mode=settings.runner_gpu_mode,
            )
            summary = profile_summary(profile)
            await progress.stage("browser_environment", f"{summary} {exit_note}".strip())
            job = await _signup_job(db, account, workspace, profile_dir, phone_pool=bool(use_phone_pool and job_id))
            launched = _new_run("signup", job, proxy_url)
            state = launched.state
            if use_phone_pool and job_id:
                state.phone_pool = True
                state.phone_lease_key = job_id
                state.phone_account_id = account.id
                state.phone_proxy_url = proxy_url
            _RUNS[state.run_id] = state
            await _launch(launched, mailbox=mailbox, profile=profile, user_data_dir=profile_dir, summary=summary)
            await progress.stage("runner_started", "已启动 Chromix 浏览器并加载插件，等待插件注册")
            deadline = time.monotonic() + settings.runner_timeout_seconds
            callback_deadline: float | None = None
            while True:
                if state.callback_url:
                    await progress.stage("runner_callback", "已收到授权回调，正在换票")
                    try:
                        await _exchange(db, account=account, ticket=ticket, callback_url=state.callback_url)
                    except OAuthSessionError as exc:
                        await _recover(db, account, workspace)
                        raise _RunFailure(OAUTH_EXCHANGE_FAILED, f"授权回调无效或会话已过期（{exc.error_code}）") from None
                    authorized = True
                    await progress.stage("runner_authorized", "已换票并写入凭据")
                    break
                _check_run(state, launched.process, deadline=deadline)
                phone_step = _PHONE_STAGE_PROGRESS.get(state.stage)
                if phone_step and f"{state.phase}:{phone_step}" not in progress.sent:
                    # Once per phase: a phone page during authorization shows even after one in signup.
                    progress.sent.add(f"{state.phase}:{phone_step}")
                    await progress.stage(phone_step)
                mapped = _STAGE_PROGRESS.get(state.stage) if state.phase == "signup" else None
                if mapped:
                    await progress.once(mapped)
                if state.phase == "oauth":
                    await progress.once("oauth")
                if state.phase == "oauth" and state.status == "done":
                    callback_deadline = callback_deadline or time.monotonic() + OAUTH_DONE_CALLBACK_GRACE
                    if time.monotonic() > callback_deadline:
                        raise _RunFailure(RUNNER_EXITED, "插件报告授权完成，但服务器没有收到回调")
                if state.phase == "signup" and state.status == "done" and state.command == "none":
                    await progress.once("runner_signup_done", "插件已完成注册，正在确认官方入组")
                    member, last_error = await _confirm_member(db, workspace, account.email, progress, state, launched.process)
                    if member is None:
                        _check_run(state, launched.process, deadline=deadline)
                        raise _RunFailure(NOT_JOINED, "注册完成但官方成员列表未确认入组" + (f"（{last_error}）" if last_error else ""))
                    joined = True
                    await ensure_membership(
                        db, workspace_id=workspace.id, account_id=account.id,
                        official_role=normalize_official_role(member.get("role")),
                        membership_state=MEMBERSHIP_STATE_JOINED, local_purpose=LOCAL_PURPOSE_CHILD,
                        joined_at=utcnow(),
                    )
                    await db.commit()
                    if (existing_invite_seat_error(requested_seat, member.get("seat_type"))
                            or not official_roles_equivalent(member.get("role"), requested_role)):
                        raise _RunFailure(MEMBERSHIP_MISMATCH, "已入组，但官方角色或席位与请求不一致，未启动授权")
                    await progress.stage("runner_joined", "官方已确认入组")
                    try:
                        ticket, url, stored = await _begin_authorize(
                            db, account=account, workspace=workspace, proxy_url=proxy_url, job_id=job_id)
                    except OAuthSessionError as exc:
                        await _recover(db, account, workspace)
                        raise _RunFailure(OAUTH_EXCHANGE_FAILED, f"无法创建授权会话（{exc.error_code}）") from None
                    state.authorize_url = url
                    state.command = "authorize"
                    state.wake.set()
                    await progress.stage("runner_authorizing", "已下发授权链接，等待插件完成授权")
                    continue
                await progress.heartbeat()
                await _wait_wake(state)
        except _RunFailure as exc:
            error_code, error = exc.code, exc.message
        except RunnerEnvironmentError as exc:
            error_code, error = exc.error_code, str(exc)
        except Exception as exc:  # noqa: BLE001 - always return an outcome; cleanup runs below
            await _recover(db, account, workspace)
            error_code, error = _unexpected(exc, launched)
    finally:
        if launched is not None:
            tail = await _shutdown(launched)
            diagnostics = _diagnostics(launched.state, summary, tail)
            if launched.state.phone_pool:
                await _release_phones(db, launched.state, account, workspace)
        if ticket:
            oauth_sessions.pop_session(ticket)
        if stored is not None:
            try:
                await oauth_session_store.finish(db, stored, success=authorized)
                await db.commit()
            except Exception:  # noqa: BLE001
                await _recover(db, account, workspace)
                logger.warning("runner oauth session could not be finished", exc_info=True)
    if diagnostics is None:
        diagnostics = {"run_kind": "signup", "profile_summary": summary}
    await progress.note("runner_diagnostics", _diagnostics_line(diagnostics, error_code), account, workspace)
    if authorized:
        return RunnerOutcome(ok=True, joined=True, authorized=True, diagnostics=diagnostics)
    return RunnerOutcome(ok=False, error_code=error_code or RUNNER_EXITED, error=error or "运行器未完成",
                         joined=joined, authorized=False, diagnostics=diagnostics)


# ---- selfcheck ----

def _prune_selfchecks(keep: str) -> None:
    try:
        folders = [p for p in SELFCHECK_ROOT.iterdir() if p.is_dir()]
    except OSError:
        return
    folders.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    kept = 0
    for folder in folders:
        if folder.name == keep:
            continue
        kept += 1
        if kept >= SELFCHECK_KEEP:
            runner_process.remove_tree(folder)


def _finding(findings: list, code: str, level: str, message: str) -> None:
    findings.append({"code": code, "level": level, "message": message})


def _ua_platform(ua: str) -> str:
    if "Windows" in ua:
        return "windows"
    if "Macintosh" in ua or "Mac OS" in ua:
        return "mac"
    if "Android" in ua:
        return "android"
    if "Linux" in ua or "X11" in ua:
        return "linux"
    return ""


def _nav_platform(value: str) -> str:
    text = str(value or "").lower()
    if text.startswith("win"):
        return "windows"
    if text.startswith("mac"):
        return "mac"
    if "linux" in text:
        return "linux"
    return ""


def _public_ip(value: str) -> ipaddress._BaseAddress | None:
    try:
        return ipaddress.ip_address(str(value).strip())
    except ValueError:
        return None


def _findings(profile: dict[str, Any], signals: dict | None, exit_info: dict | None, screenshots: dict[str, str],
              probe_names: list[str]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    exit_ip = str((exit_info or {}).get("ip") or "")
    if not exit_info or not exit_ip:
        _finding(findings, "exit_unavailable", "warn", "浏览器内未能查到出口 IP，无法比对时区和 WebRTC")
    else:
        country = str(exit_info.get("country") or "").upper()
        if country and country != profile["exit_country"]:
            _finding(findings, "exit_country_mismatch", "error", f"浏览器出口国家 {country} 与建档时 {profile['exit_country']} 不符")
        exit_tz = str(exit_info.get("timezone") or "")
        if exit_tz and exit_tz != profile["timezone"]:
            _finding(findings, "exit_timezone_mismatch", "error", f"出口 IP 所在时区 {exit_tz} 与伪装时区 {profile['timezone']} 不符")
    if not isinstance(signals, dict):
        _finding(findings, "signals_missing", "error", "没有收到浏览器信号，无法判断指纹")
        signals = {}
    else:
        tz = str(signals.get("timezone") or "")
        if tz != profile["timezone"]:
            _finding(findings, "timezone_mismatch", "error", f"页面时区 {tz or '未知'} 与伪装时区 {profile['timezone']} 不符")
        offset = signals.get("utcOffsetMinutes")
        try:
            expected = int(datetime.now(ZoneInfo(profile["timezone"])).utcoffset().total_seconds() // 60)
        except (ZoneInfoNotFoundError, ValueError, AttributeError):
            expected = None
        if isinstance(offset, (int, float)) and expected is not None and int(offset) != expected:
            _finding(findings, "utc_offset_mismatch", "error", f"页面 UTC 偏移 {int(offset)} 分钟，应为 {expected} 分钟")
        if signals.get("webdriver") is True:
            _finding(findings, "webdriver", "error", "navigator.webdriver 为 true，会被识别为自动化")
        ua = str(signals.get("userAgent") or "")
        if "HeadlessChrome" in ua:
            _finding(findings, "headless_ua", "error", "UA 中含 HeadlessChrome")
        ua_platform = _ua_platform(ua)
        nav_platform = _nav_platform(signals.get("platform"))
        ch_platform = _nav_platform((signals.get("uaData") or {}).get("platform") if isinstance(signals.get("uaData"), dict) else "")
        observed = {p for p in (ua_platform, nav_platform, ch_platform) if p}
        if len(observed) > 1:
            _finding(findings, "platform_inconsistent", "error",
                     f"UA 平台（{ua_platform or '未知'}）、navigator.platform（{nav_platform or '未知'}）、UA-CH（{ch_platform or '未知'}）不一致")
        elif observed and profile["platform"] not in observed:
            _finding(findings, "platform_mismatch", "error", f"浏览器报告平台 {observed.pop()}，档案伪装为 {profile['platform']}")
        languages = signals.get("languages") if isinstance(signals.get("languages"), list) else []
        if not languages or languages[0] != "en-US":
            _finding(findings, "language_mismatch", "warn", "navigator.languages 首选不是 en-US")
        screen = signals.get("screen") if isinstance(signals.get("screen"), dict) else {}
        window = signals.get("window") if isinstance(signals.get("window"), dict) else {}
        sw, sh = screen.get("width"), screen.get("height")
        if (sw, sh) != (profile["screen"]["width"], profile["screen"]["height"]):
            _finding(findings, "screen_mismatch", "warn", f"屏幕 {sw}x{sh} 与档案 1920x1080 不符")
        ow, oh = window.get("outerWidth"), window.get("outerHeight")
        if all(isinstance(v, (int, float)) for v in (sw, sh, ow, oh)) and (ow > sw or oh > sh):
            _finding(findings, "window_exceeds_screen", "error", f"窗口 {ow}x{oh} 大于屏幕 {sw}x{sh}")
        if signals.get("hardwareConcurrency") not in (None, profile["hardware_concurrency"]):
            _finding(findings, "hardware_mismatch", "warn", "CPU 核数与档案不符")
        if signals.get("deviceMemory") not in (None, profile["device_memory"]):
            _finding(findings, "memory_mismatch", "warn", "设备内存与档案不符")
        webgl = signals.get("webgl") if isinstance(signals.get("webgl"), dict) else {}
        renderer = f"{webgl.get('unmaskedRenderer') or ''} {webgl.get('renderer') or ''}"
        if re.search(r"swiftshader|llvmpipe|softpipe|software", renderer, re.I):
            _finding(findings, "software_webgl", "warn", "WebGL 显示软件渲染（SwiftShader / llvmpipe），容易被识别为服务器")
        elif isinstance(profile["gpu"], dict) and webgl.get("unmaskedRenderer") and webgl.get("unmaskedRenderer") != profile["gpu"]["renderer"]:
            _finding(findings, "gpu_preset_ignored", "warn", "WebGL 渲染器与预设显卡不一致")
        ips = signals.get("webrtcIps") if isinstance(signals.get("webrtcIps"), list) else []
        leaks, local = [], []
        for raw in ips[:20]:
            ip = _public_ip(raw)
            if ip is None or str(ip) == exit_ip:
                continue
            (local if (ip.is_private or ip.is_loopback or ip.is_link_local) else leaks).append(str(ip))
        if leaks:
            _finding(findings, "webrtc_leak", "error", "WebRTC 暴露了非出口公网 IP：" + "、".join(leaks[:5]))
        if local:
            _finding(findings, "webrtc_local_ip", "warn", "WebRTC 暴露了内网 IP：" + "、".join(local[:5]))
    missing = [name for name in probe_names if name not in screenshots]
    if missing:
        _finding(findings, "screenshot_missing", "warn", "缺少截图：" + "、".join(missing))
    return findings


def _exit_view(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    if data.get("error"):
        return {"error": _clean_text(data.get("error"), 120)}
    body = data.get("body") if isinstance(data.get("body"), dict) else {}
    view = {key: str(body.get(key))[:120] for key in _EXIT_KEYS if body.get(key) is not None}
    view["status"] = data.get("status")
    return view


async def run_selfcheck(db, *, proxy_url: str, platform: str | None, job_id: str) -> dict[str, Any]:
    """Launch the same browser + extension against fingerprint probe pages; no signup.

    The caller holds the global browser slot (same as ``signup_and_authorize``). Uses a
    throw-away profile under ``data/chrome-profiles/runner-selfcheck/<run_id>/``.
    """
    settings = load_settings()
    progress = _Progress(db, job_id, None)
    launched: _Launched | None = None
    profile: dict[str, Any] | None = None
    profile_dir: Path | None = None
    summary = ""
    error_code, error = "", ""
    completed = False
    diagnostics: dict[str, Any] | None = None
    probe_urls = [dict(item) for item in SELFCHECK_PROBE_URLS]
    screenshot_dir = SELFCHECK_ROOT / job_id if _JOB_ID.fullmatch(str(job_id or "")) else None
    try:
        try:
            if screenshot_dir is None:
                raise RunnerEnvironmentError(RUNNER_NOT_CONFIGURED, "自检任务编号无效")
            validate_runner_configuration()
            await check_runner_proxy(proxy_url)
            chosen = (platform or settings.runner_fingerprint_platform or "").strip().lower()
            launched_run = _new_run("selfcheck", {"kind": "selfcheck", "probeUrls": probe_urls}, proxy_url)
            state = launched_run.state
            profile_dir = SELFCHECK_PROFILES_ROOT / state.run_id
            try:
                geo = await lookup_exit(proxy_url)
            except GeoLookupError as exc:
                raise _geo_error(exc) from None
            profile = await asyncio.to_thread(create_runner_profile, profile_dir, geo=geo, platform=chosen,
                                              gpu_mode=settings.runner_gpu_mode)
            summary = profile_summary(profile)
            await progress.stage("browser_environment", f"{summary}（自检临时档案）")
            state.screenshot_dir = screenshot_dir
            await asyncio.to_thread(screenshot_dir.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(_prune_selfchecks, screenshot_dir.name)
            launched = launched_run
            _RUNS[state.run_id] = state
            # Self-check reads no mail; the run still gets the configured mailbox so the plugin loads as in production.
            from app.application.reauth import load_cf_config
            mailbox = await load_cf_config(db)
            await _launch(launched, mailbox=mailbox, profile=profile, user_data_dir=profile_dir, summary=summary)
            await progress.stage("runner_started", "已启动 Chromix 浏览器并加载插件，开始自检")
            deadline = time.monotonic() + SELFCHECK_TIMEOUT
            while True:
                if state.status == "done":
                    completed = True
                    break
                _check_run(state, launched.process, deadline=deadline)
                await progress.heartbeat()
                await _wait_wake(state)
        except _RunFailure as exc:
            error_code, error = exc.code, exc.message
        except RunnerEnvironmentError as exc:
            error_code, error = exc.error_code, str(exc)
        except Exception as exc:  # noqa: BLE001 - always return a result; cleanup runs below
            await _rollback(db)
            error_code, error = _unexpected(exc, launched)
    finally:
        if launched is not None:
            tail = await _shutdown(launched)
            diagnostics = _diagnostics(launched.state, summary, tail)
        if profile_dir is not None:
            await asyncio.to_thread(runner_process.remove_tree, profile_dir)
    state_ = launched.state if launched is not None else None
    signals = state_.probes.get("signals") if state_ else None
    exit_info = _exit_view(state_.probes.get("exit")) if state_ else None
    screenshots = dict(state_.screenshots) if state_ else {}
    findings = _findings(profile, signals, exit_info, screenshots, [p["name"] for p in probe_urls]) if profile else []
    ok = completed and not error_code and not any(item["level"] == "error" for item in findings)
    if diagnostics is None:
        diagnostics = {"run_kind": "selfcheck", "profile_summary": summary}
    plugin = diagnostics.get("plugin") if isinstance(diagnostics.get("plugin"), dict) else {}
    probe_status = plugin.get("probes") if isinstance(plugin.get("probes"), dict) else {}
    await progress.note("runner_diagnostics", _diagnostics_line(diagnostics, error_code))
    return {
        "ok": ok,
        "completed": completed,
        "error_code": error_code,
        "error": error,
        "summary": summary,
        "platform": profile["platform"] if profile else "",
        "exit": exit_info,
        "signals": signals if isinstance(signals, dict) else None,
        "screenshots": [screenshots[p["name"]] for p in probe_urls if p["name"] in screenshots],
        "findings": findings,
        "probe_status": {str(k)[:60]: str(v)[:40] for k, v in list(probe_status.items())[:10]},
        "diagnostics": diagnostics,
    }
