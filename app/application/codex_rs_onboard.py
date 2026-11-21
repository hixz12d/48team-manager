"""Batch mailbox-OTP OAuth onboarding for codex-rs, triggered by codex-rs over internal HTTP.

Flow per email (serial): check rs duplicates -> locate HME mailbox -> pick least-loaded healthy
codex-rs proxy and sample its full URL in memory -> wait for the shared browser slot -> Playwright
email-OTP login + auto-pick unique Team workspace -> code exchange -> token/plan verification ->
import as a new codex-rs account with the caller's raw settings -> read-back verification.

Tokens, OTP codes, proxy URLs and OAuth callback URLs never enter logs, DB state or HTTP responses.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import uuid
from datetime import datetime
from pathlib import Path

from app.application.codex_export import CodexTransferError
from app.application.codex_publish import _token_identity, _verify_remote
from app.application.codex_reauth import FrozenProxyClient
from app.application.codex_rs_recovery import (
    BEIJING,
    BROWSER_TIMEOUT,
    REASONS as RECOVERY_REASONS,
    _Stop,
    _client,
    _find_mailbox,
)
from app.application.jobs.browser import InvitedBrowserSession, lock as browser_lock
from app.application.operations import operation_store
from app.application.resources.hme import load_config as load_hme_config
from app.application.settings import get_setting_value, upsert_setting
from app.core.jwt import jwt_parser
from app.core.proxy import normalize_proxy_url
from app.core.time import as_utc, utcnow
from app.domain.identity.ids import normalize_email
from app.integrations.openai import oauth_sessions
from app.integrations.openai.chatgpt import chatgpt_client

logger = logging.getLogger(__name__)

STATE_KEY = "codex_rs_onboard_batch"
EXCHANGE_ID = "codex_rs_onboard"
MAX_BATCH_EMAILS = 50
MAX_SETTINGS_BYTES = 4096
BROWSER_POLL_SECONDS = 20
TEAM_PLAN_TYPES = {"team", "business", "enterprise", "edu"}
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

MANUAL_CODES = {
    "phone_verification_required",
    "cloudflare_challenge",
    "account_deactivated",
    "account_not_registered",
    "password_missing",
    "password_rejected",
    "membership_changed",
    "identity_mismatch",
    "mailbox_unknown",
    "mail_missing",
    "workspace_ambiguous",
    "team_missing",
    "not_team_workspace",
    "import_mismatch",
}

REASONS = {
    **RECOVERY_REASONS,
    "already_in_rs": "rs 里已有这个邮箱的号",
    "workspace_ambiguous": "有多个 Team，未自动选择",
    "team_missing": "只有个人空间，没有 Team",
    "not_team_workspace": "授权到的不是 Team 工作区",
    "no_proxy": "rs 没有可用代理",
    "hme_unavailable": "HME 暂时不可用",
    "codex_unavailable": "暂时连不上 rs",
    "interrupted": "48team 重启，处理中断",
    "onboard_failed": "处理异常中断",
    "import_failed": "导入 rs 失败",
    "import_mismatch": "导入 rs 后读回核对不一致",
    "desktop_session_missing": "授权页未识别已登录会话，请重试",
    "oauth_invalid_state": "授权状态失效（409 invalid_state），请重试",
    "proxy_geo_failed": "经代理查询出口时区失败，请重试",
    "registration_home_not_ready": "登录后 ChatGPT 会话未就绪，请重试",
    "onboarding_stuck": "入组欢迎页未完成，请重试",
    "camoufox_not_installed": "服务器未安装 Camoufox 浏览器",
}

STAGE_MESSAGES = {
    "browser_open": "正在打开登录页",
    "chatgpt_login": "正在登录 ChatGPT",
    "chatgpt_session": "正在确认 ChatGPT 登录态",
    "fill_email": "正在登录",
    "email_otp": "等待邮箱验证码",
    "about_you": "正在填写账号资料",
    "onboarding": "正在接受团队邀请",
    "post_signup_dwell": "已进入团队主页，稍候发起授权",
    "account_pick": "正在选择对应账号",
    "workspace": "正在选择 Team 工作空间",
}

_LOCK = asyncio.Lock()
_WORKER: asyncio.Task | None = None


class OnboardRequestError(Exception):
    def __init__(self, status_code: int, code: str, message: str, *, extra: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


def _utc_iso(dt: datetime | None = None) -> str:
    stamp = as_utc(dt or utcnow())
    return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")


def _mask_email(email: str) -> str:
    return f"{str(email or '')[:3]}***"


def _log(email: str, stage: str, code: str | None = None) -> None:
    logger.info("codex-rs onboard email=%s stage=%s code=%s", _mask_email(email), stage, code or "-")


def _is_manual_code(code: str | None) -> bool:
    text = str(code or "")
    return text in MANUAL_CODES or text.startswith("sms_")


def _is_retryable(state: str, code: str | None) -> bool:
    if state == "stopped":
        return True
    if state == "failed":
        return not _is_manual_code(code)
    return False


def _safe_code(code: str | None) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "_", str(code or "").strip())[:60]
    return cleaned or "onboard_failed"


def _reason(code: str | None) -> str:
    text = str(code or "")
    if text in REASONS:
        return REASONS[text]
    if text.startswith("sms_"):
        return "需要手机验证"
    return REASONS["onboard_failed"]


def _worker_alive() -> bool:
    return _WORKER is not None and not _WORKER.done()


# ---- batch persistence & presentation -------------------------------------------------------

async def _load_raw(db) -> dict | None:
    raw = await get_setting_value(db, STATE_KEY)
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not isinstance(data.get("items"), list):
        return None
    return data


async def _save_raw(db, batch: dict) -> None:
    await upsert_setting(db, STATE_KEY, json.dumps(batch, ensure_ascii=False), "codex-rs 邮箱自动授权当前批次")
    await db.commit()


def _recompute(batch: dict) -> dict:
    items = batch.get("items") or []
    counts = {
        "total": len(items),
        "pending": 0,
        "running": 0,
        "succeeded": 0,
        "skipped": 0,
        "failed": 0,
        "stopped": 0,
    }
    for item in items:
        st = str(item.get("state") or "")
        if st in counts:
            counts[st] += 1
        item["retryable"] = _is_retryable(st, item.get("code"))
    batch["counts"] = counts
    if counts["pending"] > 0 or counts["running"] > 0:
        if batch.get("state") != "stopping":
            batch["state"] = "running"
        batch["finishedAt"] = None
    else:
        batch["state"] = "stopped" if batch.get("state") in {"stopping", "stopped"} else "finished"
        batch["finishedAt"] = batch.get("finishedAt") or _utc_iso()
    return batch


def _public_batch(batch: dict | None) -> dict | None:
    if batch is None:
        return None
    return {
        "id": batch["id"],
        "state": batch["state"],
        "createdAt": batch["createdAt"],
        "finishedAt": batch.get("finishedAt"),
        "counts": dict(batch["counts"]),
        "items": [
            {
                "email": item["email"],
                "state": item["state"],
                "code": item.get("code"),
                "message": item["message"],
                "retryable": bool(item.get("retryable")),
                "remoteAccountId": item.get("remoteAccountId"),
                "updatedAt": item["updatedAt"],
            }
            for item in batch.get("items") or []
        ],
    }


async def _load_and_heal(db) -> dict | None:
    """Caller must hold ``_LOCK``. Settles batches stranded by a process restart."""
    batch = await _load_raw(db)
    if batch is None:
        await db.commit()
        return None
    if batch.get("state") in {"running", "stopping"} and not _worker_alive():
        now = _utc_iso()
        for item in batch.get("items") or []:
            if item.get("state") == "running":
                item.update(state="failed", code="interrupted", message=REASONS["interrupted"],
                            retryable=True, updatedAt=now)
            elif item.get("state") == "pending":
                item.update(state="stopped", code=None, message="48team 重启，未执行",
                            retryable=True, updatedAt=now)
        batch["state"] = "stopped"
        _recompute(batch)
        await _save_raw(db, batch)
        return batch
    _recompute(batch)
    await db.commit()
    return batch


# ---- input validation -----------------------------------------------------------------------

def _validate_submit_payload(payload) -> tuple[list[str], dict]:
    if not isinstance(payload, dict):
        raise OnboardRequestError(400, "invalid_request", "请求体必须是 JSON 对象", extra={"invalidEmails": []})
    raw_emails = payload.get("emails")
    if not isinstance(raw_emails, list):
        raise OnboardRequestError(400, "invalid_request", "请提供邮箱列表", extra={"invalidEmails": []})

    emails: list[str] = []
    invalid: list[str] = []
    seen_valid: set[str] = set()
    seen_invalid: set[str] = set()
    for entry in raw_emails:
        if not isinstance(entry, str):
            bad = str(entry)[:200]
            if bad not in seen_invalid:
                seen_invalid.add(bad)
                invalid.append(bad)
            continue
        trimmed = entry.strip()
        if not trimmed:
            continue
        email = trimmed.lower()
        if len(email) > 255 or any(ord(ch) < 33 for ch in email) or not _EMAIL_RE.match(email):
            bad = trimmed[:200]
            if bad not in seen_invalid:
                seen_invalid.add(bad)
                invalid.append(bad)
            continue
        if email not in seen_valid:
            seen_valid.add(email)
            emails.append(email)

    if invalid:
        raise OnboardRequestError(
            400, "invalid_request", f"有 {len(invalid)} 个邮箱格式不正确", extra={"invalidEmails": invalid}
        )
    if not emails:
        raise OnboardRequestError(400, "invalid_request", "请至少提供 1 个邮箱", extra={"invalidEmails": []})
    if len(emails) > MAX_BATCH_EMAILS:
        raise OnboardRequestError(
            400, "invalid_request", f"单批最多提交 {MAX_BATCH_EMAILS} 个邮箱", extra={"invalidEmails": []}
        )

    raw_settings = payload.get("settings")
    if not isinstance(raw_settings, dict):
        raise OnboardRequestError(400, "invalid_request", "settings 必须是 JSON 对象", extra={"invalidEmails": []})
    try:
        serialized = json.dumps(raw_settings, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError):
        raise OnboardRequestError(400, "invalid_request", "settings 不是合法的 JSON 对象",
                                  extra={"invalidEmails": []}) from None
    if len(serialized) > MAX_SETTINGS_BYTES:
        raise OnboardRequestError(400, "invalid_request", "settings 不能超过 4 KB", extra={"invalidEmails": []})
    return emails, raw_settings


def _new_batch_id(previous_id: str | None) -> str:
    stamp = utcnow().astimezone(BEIJING).strftime("%Y%m%d-%H%M%S")
    candidate = f"b-{stamp}"
    if candidate == previous_id:
        candidate = f"{candidate}-{uuid.uuid4().hex[:4]}"
    return candidate


# ---- public API called by routes ------------------------------------------------------------

async def get_batch(db) -> dict | None:
    async with _LOCK:
        batch = await _load_and_heal(db)
        return _public_batch(batch)


async def submit_batch(db, session_factory, payload) -> dict:
    emails, raw_settings = _validate_submit_payload(payload)
    async with _LOCK:
        current = await _load_and_heal(db)
        if current is not None and current.get("state") in {"running", "stopping"}:
            raise OnboardRequestError(409, "batch_running", "已有邮箱自动授权批次正在执行，请等待完成或先停止")

        client = await _client(db)
        if client is None:
            raise OnboardRequestError(503, "codex_not_configured", "48team 未配置 codex-rs 连接")
        hme_cfg = await load_hme_config(db)
        await db.commit()
        if not hme_cfg.configured:
            raise OnboardRequestError(503, "hme_not_configured", "48team 未配置 HME 邮箱服务")

        try:
            existing_accounts = await client.list_accounts()
        except CodexTransferError:
            raise OnboardRequestError(503, "codex_unavailable", REASONS["codex_unavailable"]) from None
        existing_emails = {
            normalize_email(item.get("email"))
            for item in existing_accounts
            if isinstance(item, dict) and item.get("email")
        }

        now = _utc_iso()
        items = []
        for email in emails:
            if email in existing_emails:
                items.append({
                    "email": email,
                    "state": "skipped",
                    "code": "already_in_rs",
                    "message": REASONS["already_in_rs"],
                    "retryable": False,
                    "remoteAccountId": None,
                    "updatedAt": now,
                })
            else:
                items.append({
                    "email": email,
                    "state": "pending",
                    "code": None,
                    "message": "排队中",
                    "retryable": False,
                    "remoteAccountId": None,
                    "updatedAt": now,
                })

        batch = {
            "id": _new_batch_id(current.get("id") if current else None),
            "state": "running",
            "createdAt": now,
            "finishedAt": None,
            "settings": raw_settings,
            "counts": {},
            "items": items,
        }
        _recompute(batch)
        await _save_raw(db, batch)
        if batch["state"] == "running":
            _spawn_worker(session_factory, batch["id"])
        return _public_batch(batch)


async def stop_batch(db) -> dict:
    async with _LOCK:
        batch = await _load_and_heal(db)
        if batch is None:
            raise OnboardRequestError(404, "no_batch", "当前没有正在执行或历史批次")
        if batch.get("state") in {"stopped", "finished"}:
            return _public_batch(batch)
        now = _utc_iso()
        for item in batch.get("items") or []:
            if item.get("state") == "pending":
                item.update(state="stopped", code=None, message="已手动停止", retryable=True, updatedAt=now)
        batch["state"] = "stopping"
        _recompute(batch)
        await _save_raw(db, batch)
        return _public_batch(batch)


async def retry_item(db, session_factory, payload) -> dict:
    if not isinstance(payload, dict) or not isinstance(payload.get("email"), str):
        raise OnboardRequestError(400, "invalid_request", "请提供要重试的邮箱")
    target_email = normalize_email(payload.get("email"))
    if not target_email:
        raise OnboardRequestError(400, "invalid_request", "请提供要重试的邮箱")

    async with _LOCK:
        batch = await _load_and_heal(db)
        if batch is None:
            raise OnboardRequestError(404, "item_not_found", "当前批次中没有这个邮箱")
        item = next((row for row in batch.get("items") or [] if row.get("email") == target_email), None)
        if item is None:
            raise OnboardRequestError(404, "item_not_found", "当前批次中没有这个邮箱")
        if batch.get("state") == "stopping" or not item.get("retryable"):
            raise OnboardRequestError(409, "not_retryable", "该账号当前不可重试")

        now = _utc_iso()
        item.update(
            state="pending",
            code=None,
            message="排队中",
            retryable=False,
            remoteAccountId=None,
            updatedAt=now,
        )
        batch["state"] = "running"
        batch["finishedAt"] = None
        _recompute(batch)
        await _save_raw(db, batch)
        if not _worker_alive():
            _spawn_worker(session_factory, batch["id"])
        return _public_batch(batch)


# ---- background worker ----------------------------------------------------------------------

def _spawn_worker(session_factory, batch_id: str) -> None:
    global _WORKER
    _WORKER = asyncio.create_task(_worker_loop(session_factory, batch_id), name=f"codex-rs-onboard-{batch_id}")


async def _claim_next_pending(session_factory, batch_id: str) -> tuple[str | None, dict]:
    async with _LOCK:
        async with session_factory() as db:
            batch = await _load_raw(db)
            if batch is None or batch.get("id") != batch_id:
                await db.commit()
                return None, {}
            now = _utc_iso()
            if batch.get("state") in {"stopping", "stopped"}:
                for row in batch.get("items") or []:
                    if row.get("state") == "pending":
                        row.update(state="stopped", code=None, message="已手动停止", retryable=True, updatedAt=now)
                _recompute(batch)
                await _save_raw(db, batch)
                return None, {}
            target = next((row for row in batch.get("items") or [] if row.get("state") == "pending"), None)
            if target is None:
                _recompute(batch)
                await _save_raw(db, batch)
                return None, {}
            target.update(state="running", code=None, message="正在检查 rs 账号", retryable=False, updatedAt=now)
            _recompute(batch)
            raw_settings = dict(batch.get("settings") or {})
            await _save_raw(db, batch)
            return str(target["email"]), raw_settings


async def _is_stopping(session_factory, batch_id: str) -> bool:
    async with _LOCK:
        async with session_factory() as db:
            batch = await _load_raw(db)
            await db.commit()
            return batch is None or batch.get("id") != batch_id or batch.get("state") in {"stopping", "stopped"}


async def _update_item(session_factory, batch_id: str, email: str, **changes) -> None:
    async with _LOCK:
        async with session_factory() as db:
            batch = await _load_raw(db)
            if batch is None or batch.get("id") != batch_id:
                await db.commit()
                return
            target = next((row for row in batch.get("items") or [] if row.get("email") == email), None)
            if target is None:
                await db.commit()
                return
            target.update(**changes, updatedAt=_utc_iso())
            _recompute(batch)
            await _save_raw(db, batch)


async def _set_running_message(session_factory, batch_id: str, email: str, message: str) -> None:
    async with _LOCK:
        async with session_factory() as db:
            batch = await _load_raw(db)
            if batch is None or batch.get("id") != batch_id:
                await db.commit()
                return
            target = next((row for row in batch.get("items") or [] if row.get("email") == email), None)
            if target is None or target.get("state") != "running" or target.get("message") == message:
                await db.commit()
                return
            target.update(message=message, updatedAt=_utc_iso())
            await _save_raw(db, batch)


async def _pick_proxy(client) -> tuple[str, str]:
    """Return ``(proxy_id, full_proxy_url)``; raises ``_Stop`` when none can be obtained."""
    try:
        proxies = await client.list_proxies()
    except CodexTransferError:
        raise _Stop("codex_unavailable") from None

    candidates = [
        item for item in proxies
        if item.get("enabled") is True
        and isinstance(item.get("lastTest"), dict)
        and item["lastTest"].get("success") is True
        and type(item.get("accountCount")) is int
        and item["accountCount"] >= 1
        and isinstance(item.get("id"), str)
        and item["id"]
    ]
    random.shuffle(candidates)
    candidates.sort(key=lambda item: item["accountCount"])

    for cand in candidates:
        proxy_id = cand["id"]
        try:
            linked = await client.list_proxy_accounts(proxy_id, page=1, page_size=1)
            if not linked:
                continue
            sample_id = linked[0].get("id")
            if not isinstance(sample_id, str) or not sample_id:
                continue
            proxy = normalize_proxy_url(await client.export_proxy_url(sample_id))
            if proxy:
                return proxy_id, proxy
        except (CodexTransferError, ValueError):
            continue
    raise _Stop("no_proxy")


async def _wait_for_browser_slot(session_factory, batch_id: str, email: str,
                                 browser: InvitedBrowserSession) -> bool:
    """Reserve the shared Playwright slot; returns False if the batch was stopped while waiting."""
    while True:
        if await _is_stopping(session_factory, batch_id):
            return False
        busy = browser_lock().locked()
        if not busy:
            async with session_factory() as db:
                busy = (await operation_store.browser_busy(db)) is not None
                await db.commit()
        if not busy and await browser.try_reserve():
            return True
        await _set_running_message(session_factory, batch_id, email, "等待浏览器空闲")
        for _ in range(BROWSER_POLL_SECONDS):
            if await _is_stopping(session_factory, batch_id):
                return False
            await asyncio.sleep(1)


async def _process_one(session_factory, batch_id: str, email: str, raw_settings: dict) -> None:
    browser = InvitedBrowserSession()
    exchanger = None
    ticket = None
    proxy = ""
    try:
        # 1. Re-check codex-rs in case the email was imported since batch submission.
        async with session_factory() as db:
            client = await _client(db)
        if client is None:
            raise _Stop("codex_not_configured")
        try:
            accounts = await client.list_accounts()
        except CodexTransferError:
            raise _Stop("codex_unavailable") from None
        if any(normalize_email(acc.get("email")) == email for acc in accounts if isinstance(acc, dict)):
            _log(email, "skip", "already_in_rs")
            await _update_item(session_factory, batch_id, email, state="skipped", code="already_in_rs",
                               message=REASONS["already_in_rs"], remoteAccountId=None)
            return

        # 2. Locate mailbox in HME.
        await _set_running_message(session_factory, batch_id, email, "正在查找验证码邮箱")
        async with session_factory() as db:
            mailbox = await _find_mailbox(db, email)
        if mailbox is None:
            raise _Stop("hme_unavailable")
        hme, hme_account_id = mailbox

        # 3. Pick least-loaded healthy proxy and fetch its full URL from one bound account.
        await _set_running_message(session_factory, batch_id, email, "正在挑选代理")
        proxy_id, proxy = await _pick_proxy(client)

        # 4. Wait for the single shared browser slot.
        if not await _wait_for_browser_slot(session_factory, batch_id, email, browser):
            _log(email, "stopped")
            await _update_item(session_factory, batch_id, email, state="stopped", code=None,
                               message="已手动停止", remoteAccountId=None)
            return

        # 5. Browser email-OTP login + unique Team selection + OAuth code exchange.
        await _set_running_message(session_factory, batch_id, email, "正在登录")
        auth = chatgpt_client.create_oauth_authorize_url(
            oauth_sessions.CLIENT_ID, oauth_sessions.REDIRECT_URI, login_hint=email, entry="desktop"
        )
        ticket = oauth_sessions.create_session(
            team_id=0, email=email, authorize=auth, mode="auto", proxy=proxy
        )["ticket"]
        exchanger = FrozenProxyClient(proxy)

        async def on_stage(stage: str, _msg: str) -> None:
            label = STAGE_MESSAGES.get(stage)
            if label:
                await _set_running_message(session_factory, batch_id, email, label)

        _log(email, "browser")
        async with asyncio.timeout(BROWSER_TIMEOUT):
            result = await browser.run(
                email=email,
                password="",
                authorize_url=auth["authorize_url"],
                proxy=proxy,
                login_only=True,
                allow_signup=False,
                allow_profile_setup=True,
                allow_onboarding=True,
                allow_join=False,
                allow_sms=False,
                team_name="",
                pick_single_team=True,
                use_cloudflare=False,
                hme_base_url=hme.base_url,
                hme_service_token=hme.service_token,
                hme_account_id=hme_account_id,
                on_stage=on_stage,
            )
        if not result.get("ok"):
            err_code = str(result.get("error_code") or "browser_failed")
            debug_dir = Path(str(result.get("debug_dir") or "")).name
            if debug_dir:
                logger.info(
                    "codex-rs onboard email=%s stage=browser_debug dir=%s code=%s",
                    _mask_email(email), debug_dir, err_code,
                )
            result.clear()
            raise _Stop(err_code)

        session = oauth_sessions.get_session(ticket) or {}
        parsed = oauth_sessions.parse_oauth_callback(result.get("callback_url") or "")
        result.clear()
        if not parsed["code"] or parsed["error"] or not session or parsed["state"] != session.get("state"):
            parsed.clear()
            raise _Stop("oauth_callback_invalid")

        await _set_running_message(session_factory, batch_id, email, "正在换取令牌")
        _log(email, "exchange")
        async with session_factory() as db:
            exchanged = await exchanger.exchange_oauth_code(
                parsed["code"],
                oauth_sessions.CLIENT_ID,
                oauth_sessions.REDIRECT_URI,
                session.get("code_verifier") or "",
                db,
                identifier=EXCHANGE_ID,
            )
            await db.commit()
        parsed.clear()
        if not exchanged.get("success"):
            exchanged.clear()
            raise _Stop("oauth_exchange_failed")

        # 6. Verify identity, refresh token, workspace ID and Team plan claim.
        access_token = str(exchanged.get("access_token") or "")
        refresh_token = str(exchanged.get("refresh_token") or "")
        id_token = str(exchanged.get("id_token") or "") or None
        exchanged.clear()

        identity = _token_identity(access_token) or {}
        auth_claim = jwt_parser.extract_auth_claim(access_token)
        plan_type = str(auth_claim.get("chatgpt_plan_type") or "").strip().lower()
        if not refresh_token or identity.get("email") != email:
            access_token = refresh_token = ""
            id_token = None
            raise _Stop("identity_mismatch")
        if not identity.get("account_id") or plan_type not in TEAM_PLAN_TYPES:
            access_token = refresh_token = ""
            id_token = None
            raise _Stop("not_team_workspace")

        # 7. Import into codex-rs with caller settings + chosen proxy, then verify by reading back.
        await _set_running_message(session_factory, batch_id, email, "正在导入 rs")
        document = {
            "provider": "openai",
            "email": identity["email"],
            "access_token": access_token,
            "refresh_token": refresh_token,
            "client_id": oauth_sessions.CLIENT_ID,
        }
        if id_token:
            document["id_token"] = id_token
        access_token = refresh_token = ""
        id_token = None

        _log(email, "import")
        try:
            imported_ids = await client.import_accounts(document, raw_settings=raw_settings, proxy_id=proxy_id)
        except CodexTransferError:
            raise _Stop("import_failed") from None
        finally:
            document.clear()
        remote_id = imported_ids[0]

        try:
            remote = await client.detail(remote_id)
        except CodexTransferError:
            raise _Stop("codex_unavailable") from None
        try:
            _verify_remote(remote, identity)
        except CodexTransferError:
            raise _Stop("import_mismatch") from None

        # 8. Mark succeeded.
        _log(email, "succeeded")
        await _update_item(session_factory, batch_id, email, state="succeeded", code=None,
                           message="已导入 rs", remoteAccountId=remote_id)
    except _Stop as stop:
        code = _safe_code(stop.code)
        _log(email, "failed", code)
        await _update_item(session_factory, batch_id, email, state="failed", code=code,
                           message=_reason(code), remoteAccountId=None)
    except TimeoutError:
        _log(email, "failed", "browser_timeout")
        await _update_item(session_factory, batch_id, email, state="failed", code="browser_timeout",
                           message=_reason("browser_timeout"), remoteAccountId=None)
    except Exception as exc:  # noqa: BLE001 - worker continues with remaining items
        logger.warning("codex-rs onboard failed email=%s error=%s", _mask_email(email), type(exc).__name__)
        await _update_item(session_factory, batch_id, email, state="failed", code="onboard_failed",
                           message=REASONS["onboard_failed"], remoteAccountId=None)
    finally:
        proxy = ""  # noqa: F841 - drop reference immediately
        await browser.close()
        if exchanger is not None:
            await exchanger.clear_session(EXCHANGE_ID)
        if ticket:
            oauth_sessions.pop_session(ticket)


async def _worker_loop(session_factory, batch_id: str) -> None:
    while True:
        email, raw_settings = await _claim_next_pending(session_factory, batch_id)
        if email is None:
            return
        await _process_one(session_factory, batch_id, email, raw_settings)
