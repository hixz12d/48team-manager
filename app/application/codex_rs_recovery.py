"""Recover logged-out codex-rs accounts in place by mailbox OTP, whether or not Team48 imported them.

Order per account: codex-rs RT refresh -> browser login with the account's own proxy and HME
mailbox -> Team48 code exchange -> identity check -> import upsert onto the same codex-rs ID.
Tokens, proxy URLs, callback URLs and OTP codes never reach logs, state JSON or results; logs
carry only the remote ID tail, the stage and an error code.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select

from app.application.codex_export import CodexTransferError
from app.application.jobs.browser import InvitedBrowserSession, lock as browser_lock
from app.application.jobs.commands import command_runner
from app.application.operations import operation_store
from app.application.resources.hme import HmeError, hme_client, load_config as load_hme_config
from app.application.settings import get_setting_value, upsert_setting
from app.core.proxy import normalize_proxy_url
from app.core.time import as_utc, isoformat, utcnow
from app.domain.identity.ids import normalize_email
from app.integrations.codex.client import CodexClient, CodexRemoteError, account_group_ids, load_config
from app.integrations.openai import oauth_sessions
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.models.codex import CodexBinding
from app.persistence.models.identity import Workspace

logger = logging.getLogger(__name__)

SETTING_KEY = "codex_rs_recovery_enabled"
STATE_KEY = "codex_rs_recovery_state"
CANDIDATE_REASONS = {"access_token_expired", "credential_expired", "credential_invalid"}
HEALTHY_STATUSES = {"normal", "rate_limited", "quota_exhausted"}
# codex-rs refresh codes that prove the stored RT cannot recover the account: go to the browser.
BROWSER_REFRESH_CODES = {40001, 50201}
MARK = "[自动重授权]"
NOTES_LIMIT = 500
MAX_FAILS = 3
RETRY_STEP = timedelta(minutes=15)
SKIP_DELAY = timedelta(minutes=5)
SUCCESS_COOLDOWN = timedelta(minutes=30)
STALE_RUNNING = timedelta(minutes=20)
BROWSER_TIMEOUT = 900
BEIJING = timezone(timedelta(hours=8))
EXCHANGE_ID = "codex_rs_recovery"

MANUAL_CODES = {"phone_verification_required", "cloudflare_challenge", "account_deactivated",
                "account_not_registered", "password_missing", "membership_changed", "identity_mismatch",
                "proxy_missing", "mailbox_unknown", "mail_missing", "import_mismatch"}
REASONS = {
    "phone_verification_required": "需要手机验证",
    "cloudflare_challenge": "遇到人机验证",
    "account_deactivated": "账号已被封",
    "account_not_registered": "账号不存在或未注册",
    "password_missing": "登录页没有邮箱验证码入口",
    "password_rejected": "登录页没有邮箱验证码入口",
    "membership_changed": "需要接受团队邀请，未自动加入",
    "identity_mismatch": "工作空间或身份不一致",
    "import_mismatch": "写回时 rs 生成了另一个账号，请人工核对",
    "proxy_missing": "没有绑定代理",
    "mailbox_unknown": "找不到验证码邮箱",
    "mail_missing": "HME 未配置，读不到验证码",
    "mail_otp_timeout": "等邮箱验证码超时",
    "mail_otp_rejected": "邮箱验证码未通过",
    "about_you_stuck": "卡在年龄/生日页",
    "oauth_callback_missing": "没拿到授权回调",
    "oauth_callback_invalid": "授权回调无效",
    "oauth_invalid_state": "授权状态失效（409 invalid_state）",
    "oauth_exchange_failed": "换取令牌失败",
    "desktop_session_missing": "授权页未识别已登录会话",
    "registration_home_not_ready": "登录后 ChatGPT 会话未就绪",
    "proxy_geo_failed": "经代理查询出口时区失败",
    "browser_failed": "浏览器出错",
    "browser_timeout": "浏览器超时",
    "browser_worker_exited": "浏览器进程异常退出",
    "browser_worker_failed": "浏览器进程异常退出",
    "browser_environment_invalid": "浏览器环境不可用",
    "import_failed": "写回 rs 失败",
    "verify_failed": "写回后 rs 状态未恢复",
    "recovery_failed": "处理异常中断",
    # Skips: this round only, never counted as failures.
    "disabled": "自动重授权开关已关闭",
    "codex_not_configured": "codex-rs 连接未配置",
    "remote_unavailable": "暂时读不到 rs 账号",
    "not_candidate": "rs 账号当前不是掉登录状态",
    "legacy_binding": "该号由 48team 旧的重授权功能负责",
    "refresh_skipped": "rs 刷新忙或结果未知，下轮再试",
    "hme_unavailable": "HME 暂时不可用",
    "browser_busy": "浏览器正被占用，等下一轮",
    "already_running": "该号正在处理中",
    "changed_during_recovery": "处理期间 rs 账号已变化，未写回",
}
STATE_LABELS = {"running": "正在重新授权", "retry_wait": "等待重试", "manual_required": "需人工",
                "succeeded": "已恢复"}

_LOCK = asyncio.Lock()


class _Stop(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _reason(code: str | None) -> str:
    return REASONS.get(str(code or ""), str(code or "未知原因"))


def _tail(remote_id: str) -> str:
    return str(remote_id or "")[-6:]


def _log(remote_id: str, stage: str, code: str | None = None) -> None:
    logger.info("codex-rs recovery remote=...%s stage=%s code=%s", _tail(remote_id), stage, code or "-")


def _parse_time(value) -> datetime | None:
    try:
        return as_utc(datetime.fromisoformat(value)) if isinstance(value, str) and value else None
    except ValueError:
        return None


def _is_candidate(remote: dict) -> bool:
    return (remote.get("enabled") is True and remote.get("status") == "error"
            and remote.get("errorReason") in CANDIDATE_REASONS)


def _identity(remote: dict) -> tuple:
    return normalize_email(remote.get("email")), remote.get("userId"), remote.get("accountId")


def _settings(remote: dict) -> dict:
    return {"groupIds": sorted(account_group_ids(remote)), "outboundProxyId": remote.get("outboundProxyId"),
            "outboundProxyEndpoint": remote.get("outboundProxyEndpoint"),
            "concurrencyLimit": remote.get("concurrencyLimit"), "weight": remote.get("weight")}


# ---- notes ---------------------------------------------------------------------------------

def _has_mark(notes) -> bool:
    return any(line.lstrip().startswith(MARK) for line in str(notes or "").splitlines())


def _strip_mark(notes) -> str:
    return "\n".join(line for line in str(notes or "").splitlines() if not line.lstrip().startswith(MARK)).strip()


def _with_mark(notes, line: str) -> str | None:
    """User lines kept as-is; the mark line is shortened to fit 500 chars, or dropped (None) if it cannot."""
    base = _strip_mark(notes)
    room = NOTES_LIMIT - len(base) - (1 if base else 0)
    if room <= len(MARK):
        return None
    line = line[:room]
    return f"{base}\n{line}" if base else line


async def _write_notes(client: CodexClient, remote_id: str, build, *, unchanged_ok: bool = True) -> bool:
    """Rewrite the notes from a fresh detail; True when the wanted notes are in place.

    Failures only log: they never change the recovery state.
    """
    try:
        current = await client.detail(remote_id)
        notes = build(current.get("notes"))
        if notes is None:
            return unchanged_ok
        if notes != str(current.get("notes") or "").strip():
            await client.update_notes(remote_id, notes, current)
        return True
    except (CodexTransferError, ValueError) as exc:
        logger.warning("codex-rs recovery notes write failed remote=...%s code=%s",
                       _tail(remote_id), getattr(exc, "code", type(exc).__name__))
        return False


async def _clear_mark(client: CodexClient, remote_id: str) -> None:
    await _write_notes(client, remote_id, lambda notes: _strip_mark(notes) if _has_mark(notes) else None)


async def _set_mark(client: CodexClient, remote_id: str, reason: str) -> bool:
    stamp = utcnow().astimezone(BEIJING).strftime("%m-%d %H:%M")
    line = f"{MARK} 需人工：{reason}（北京时间 {stamp}）"
    return await _write_notes(client, remote_id, lambda notes: _with_mark(notes, line), unchanged_ok=False)


def _manual_reason(entry: dict) -> str:
    code = entry.get("last_code")
    count = int(entry.get("fail_count") or 0)
    if count >= MAX_FAILS and code not in MANUAL_CODES:
        return f"连续 {count} 次失败：{_reason(code)}"
    return _reason(code)


# ---- local state ---------------------------------------------------------------------------

async def _load_state(db) -> dict:
    try:
        data = json.loads(await get_setting_value(db, STATE_KEY, "{}") or "{}")
    except (TypeError, ValueError):
        data = {}
    data = data if isinstance(data, dict) else {}
    accounts = data.get("accounts")
    data["accounts"] = {str(k): v for k, v in accounts.items() if isinstance(v, dict)} if isinstance(accounts, dict) else {}
    return data


async def _save_state(db, data: dict) -> None:
    await upsert_setting(db, STATE_KEY, json.dumps(data, ensure_ascii=False), "codex-rs 掉登录自动重授权状态")
    await db.commit()


async def _entry(db, remote_id: str) -> dict | None:
    entry = (await _load_state(db))["accounts"].get(remote_id)
    await db.commit()
    return dict(entry) if entry else None


async def _put(db, remote_id: str, entry: dict | None) -> None:
    """Re-read the state before writing, so the scan and the command line never drop each other's rows."""
    data = await _load_state(db)
    if entry is None:
        data["accounts"].pop(remote_id, None)
    else:
        data["accounts"][remote_id] = {**entry, "updated_at": isoformat(utcnow())}
    await _save_state(db, data)


def _new_entry(email: str, previous: dict | None) -> dict:
    base = {"email": email, "state": "retry_wait", "fail_count": 0, "next_at": None, "last_code": None,
            "updated_at": None, "succeeded_at": None}
    return {**base, **(previous or {}), "email": email or (previous or {}).get("email") or ""}


# ---- outcomes ------------------------------------------------------------------------------

def _result(ok: bool, code: str | None, state: str | None, *, skipped=False, message: str | None = None) -> dict:
    return {"ok": ok, "skipped": skipped, "code": code, "state": state,
            "message": message or ("重新授权完成，rs 原账号已更新并核对" if ok else _reason(code))}


async def _skip(db, remote_id: str, email: str, previous: dict | None, code: str, *, delay=True) -> dict:
    """Not counted as a failure. ``delay`` parks the account for a few minutes so others get their turn."""
    _log(remote_id, "skip", code)
    if delay:
        entry = _new_entry(email, previous)
        if entry["state"] == "running":
            entry["state"] = "retry_wait"
        entry.update(next_at=isoformat(utcnow() + SKIP_DELAY), last_code=code)
        await _put(db, remote_id, entry)
    return _result(False, code, (previous or {}).get("state"), skipped=True)


async def _succeed(db, client: CodexClient, remote_id: str, email: str, previous: dict | None,
                   note: str | None = None) -> dict:
    now = utcnow()
    entry = _new_entry(email, previous)
    entry.update(state="succeeded", fail_count=0, last_code=None, succeeded_at=isoformat(now),
                 next_at=isoformat(now + SUCCESS_COOLDOWN), marked=False)
    await _put(db, remote_id, entry)
    await _clear_mark(client, remote_id)
    _log(remote_id, "succeeded")
    return _result(True, None, "succeeded", message=note)


async def _fail(db, client: CodexClient, remote_id: str, email: str, previous: dict | None, code: str) -> dict:
    entry = _new_entry(email, previous)
    entry["marked"] = False
    if code in MANUAL_CODES:
        entry.update(state="manual_required", last_code=code, next_at=None)
    else:
        count = int(entry.get("fail_count") or 0) + 1
        entry.update(fail_count=count, last_code=code)
        if count >= MAX_FAILS:
            entry.update(state="manual_required", next_at=None)
        else:
            entry.update(state="retry_wait", next_at=isoformat(utcnow() + RETRY_STEP * count))
    if entry["state"] == "manual_required":
        # Only a written mark line can later be "deleted by the user" to re-arm the account.
        entry["marked"] = await _set_mark(client, remote_id, _manual_reason(entry))
    await _put(db, remote_id, entry)
    _log(remote_id, entry["state"], code)
    return _result(False, code, entry["state"])


# ---- helpers -------------------------------------------------------------------------------

async def enabled(db) -> bool:
    return await get_setting_value(db, SETTING_KEY, "false") == "true"


async def _client(db) -> CodexClient | None:
    try:
        cfg = await load_config(db)
        await db.commit()
        return CodexClient(cfg["base_url"], cfg["api_key"])
    except (CodexTransferError, ValueError):
        await db.commit()
        return None


async def _legacy_ids(db, remote_ids) -> set[str]:
    ids = [item for item in remote_ids if item]
    if not ids:
        return set()
    rows = await db.scalars(select(CodexBinding.remote_account_id).where(CodexBinding.remote_account_id.in_(ids)))
    found = {str(item) for item in rows if item}
    await db.commit()
    return found


async def _team_name(db, official_workspace_id) -> str:
    if not official_workspace_id:
        return ""
    rows = list(await db.scalars(select(Workspace).where(Workspace.official_workspace_id == official_workspace_id)
                                 .order_by(Workspace.id.desc())))
    await db.commit()
    rows.sort(key=lambda row: row.status != "active")
    for row in rows:
        name = (row.official_name or row.name or "").strip()
        if name:
            return name
    return ""


def _hme_owner(cfg, email: str) -> str:
    """The HME account whose alias list holds this mailbox; empty when none does. Sync HTTP."""
    wanted = normalize_email(email)
    for account in hme_client.list_accounts(cfg):
        account_id = str(account.get("id") or "").strip()
        if not account_id:
            continue
        if any(normalize_email(alias.get("email")) == wanted for alias in hme_client.list_aliases(cfg, account_id)):
            return account_id
    return ""


async def _find_mailbox(db, email: str):
    """-> (hme_config, hme_account_id); raises _Stop for manual cases and returns None on a transient error."""
    cfg = await load_hme_config(db)
    await db.commit()
    if not cfg.configured:
        raise _Stop("mail_missing")
    if not email:
        raise _Stop("mailbox_unknown")
    try:
        owner = await asyncio.to_thread(_hme_owner, cfg, email)
    except HmeError as exc:
        if exc.code in {"hme_unconfigured", "hme_auth"}:
            raise _Stop("mail_missing") from None
        return None
    except Exception:  # noqa: BLE001 - network errors of the sync client
        return None
    if not owner:
        raise _Stop("mailbox_unknown")
    return cfg, owner


async def _proxy(client: CodexClient, remote_id: str) -> str:
    """Raises CodexTransferError when unreadable and _Stop("proxy_missing") when the account has none."""
    try:
        proxy = normalize_proxy_url(await client.export_proxy_url(remote_id))
    except ValueError:
        proxy = None
    if not proxy:
        raise _Stop("proxy_missing")
    return proxy


def _running(entry: dict | None) -> bool:
    if not entry or entry.get("state") != "running":
        return False
    updated = _parse_time(entry.get("updated_at"))
    return updated is not None and utcnow() - updated < STALE_RUNNING


# ---- one account ---------------------------------------------------------------------------

async def recover_account(db, remote_id: str, *, force: bool = False) -> dict:
    """Recover one codex-rs account. ``force`` ignores the switch, back-off and the manual state.

    Returns {"ok", "skipped", "code", "state", "message"}; never raises.
    """
    remote_id = str(remote_id or "").strip()
    if _LOCK.locked():
        return _result(False, "already_running", None, skipped=True)
    async with _LOCK:
        try:
            return await _recover(db, remote_id, force=force)
        except Exception as exc:  # noqa: BLE001 - callers rely on a result
            await db.rollback()
            logger.warning("codex-rs recovery crashed remote=...%s error=%s", _tail(remote_id), type(exc).__name__)
            return _result(False, "recovery_failed", None)


async def _recover(db, remote_id: str, *, force: bool) -> dict:
    if not force and not await enabled(db):
        await db.commit()
        return _result(False, "disabled", None, skipped=True)
    client = await _client(db)
    if client is None:
        return _result(False, "codex_not_configured", None, skipped=True)
    previous = await _entry(db, remote_id)
    if _running(previous):
        return _result(False, "already_running", "running", skipped=True)
    try:
        before = await client.detail(remote_id)
    except CodexTransferError:
        return _result(False, "remote_unavailable", None, skipped=True)
    email = normalize_email(before.get("email"))
    if not _is_candidate(before):
        if before.get("status") != "error" and previous is not None and previous.get("state") != "succeeded":
            await _put(db, remote_id, None)
            await _clear_mark(client, remote_id)
        return _result(False, "not_candidate", None, skipped=True)
    if await _legacy_ids(db, [remote_id]):
        return _result(False, "legacy_binding", None, skipped=True)
    if force and previous and previous.get("state") == "manual_required":
        previous = {**previous, "state": "retry_wait", "fail_count": 0, "marked": False}

    # 1. Let codex-rs try its stored RT first.
    _log(remote_id, "refresh")
    try:
        await client.refresh(remote_id)
        after = await client.detail(remote_id)
        if after.get("enabled") is True and after.get("status") in HEALTHY_STATUSES:
            return await _succeed(db, client, remote_id, email, previous, "rs 已用刷新令牌恢复，无需浏览器")
    except CodexRemoteError as exc:
        if exc.remote_code not in BROWSER_REFRESH_CODES:
            return await _skip(db, remote_id, email, previous, "refresh_skipped")
    except CodexTransferError:
        return await _skip(db, remote_id, email, previous, "remote_unavailable")

    # 2. Materials: proxy, mailbox, team name.
    try:
        proxy = await _proxy(client, remote_id)
        mailbox = await _find_mailbox(db, email)
    except _Stop as stop:
        return await _fail(db, client, remote_id, email, previous, stop.code)
    except CodexTransferError:
        return await _skip(db, remote_id, email, previous, "remote_unavailable")
    if mailbox is None:
        return await _skip(db, remote_id, email, previous, "hme_unavailable")
    hme, hme_account_id = mailbox
    team_name = await _team_name(db, before.get("accountId"))

    # 3. The global browser slot, shared with every other Team48 browser task.
    busy = await operation_store.browser_busy(db)
    await db.commit()
    if busy is not None:
        return await _skip(db, remote_id, email, previous, "browser_busy", delay=False)
    browser = InvitedBrowserSession()
    if not await browser.try_reserve():
        return await _skip(db, remote_id, email, previous, "browser_busy", delay=False)
    ticket = None
    exchanger = None
    try:
        await _put(db, remote_id, {**_new_entry(email, previous), "state": "running"})
        tokens = await _authorize(db, browser, remote_id, email, proxy, team_name, hme, hme_account_id, before)
        exchanger = tokens.pop("_exchanger")
        ticket = tokens.pop("_ticket")

        # 4. Re-check right before writing: a manual fix in the meantime wins.
        current = await client.detail(remote_id)
        if not _is_candidate(current) or _identity(current) != _identity(before):
            await _put(db, remote_id, previous)
            if current.get("status") != "error":
                await _clear_mark(client, remote_id)
            return _result(False, "changed_during_recovery", (previous or {}).get("state"), skipped=True)
        document = {"provider": "openai", "email": tokens["email"], "access_token": tokens["access_token"],
                    "refresh_token": tokens["refresh_token"], "client_id": oauth_sessions.CLIENT_ID}
        if tokens.get("id_token"):
            document["id_token"] = tokens["id_token"]
        tokens.clear()
        _log(remote_id, "import")
        try:
            imported = await client.import_accounts(document)
        except CodexTransferError:
            raise _Stop("import_failed") from None
        finally:
            document.clear()
        if imported[0] != remote_id:
            raise _Stop("import_mismatch")

        # 5. Verify, restoring settings the import may have touched.
        note = await _restore_settings(client, remote_id, before, proxy)
        after = await client.detail(remote_id)
        if (after.get("enabled") is not True or after.get("status") not in HEALTHY_STATUSES
                or _identity(after) != _identity(before) or after.get("hasRefreshToken") is not True):
            raise _Stop("verify_failed")
        return await _succeed(db, client, remote_id, email, previous, note)
    except _Stop as stop:
        return await _fail(db, client, remote_id, email, previous, stop.code)
    except TimeoutError:
        return await _fail(db, client, remote_id, email, previous, "browser_timeout")
    except CodexTransferError:
        return await _fail(db, client, remote_id, email, previous, "verify_failed")
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        logger.warning("codex-rs recovery failed remote=...%s error=%s", _tail(remote_id), type(exc).__name__)
        return await _fail(db, client, remote_id, email, previous, "recovery_failed")
    finally:
        await browser.close()
        if exchanger is not None:
            await exchanger.clear_session(EXCHANGE_ID)
        if ticket:
            oauth_sessions.pop_session(ticket)


async def _authorize(db, browser, remote_id, email, proxy, team_name, hme, hme_account_id, before) -> dict:
    """Browser login + code exchange; returns verified tokens or raises _Stop / TimeoutError."""
    from app.application.codex_publish import _token_identity
    from app.application.codex_reauth import FrozenProxyClient

    auth = chatgpt_client.create_oauth_authorize_url(
        oauth_sessions.CLIENT_ID, oauth_sessions.REDIRECT_URI, login_hint=email, entry="desktop"
    )
    # In-memory session only: there is no local account to attach an OAuthSession row to.
    ticket = oauth_sessions.create_session(team_id=0, email=email, authorize=auth, mode="auto", proxy=proxy)["ticket"]
    exchanger = FrozenProxyClient(proxy)
    holder = {"_ticket": ticket, "_exchanger": exchanger}
    try:
        _log(remote_id, "browser")
        async with asyncio.timeout(BROWSER_TIMEOUT):
            result = await browser.run(email=email, password="", authorize_url=auth["authorize_url"], proxy=proxy,
                                       login_only=True, allow_signup=False, allow_join=False, allow_sms=False,
                                       team_name=team_name, use_cloudflare=False, hme_base_url=hme.base_url,
                                       hme_service_token=hme.service_token, hme_account_id=hme_account_id)
        if not result.get("ok"):
            err_code = str(result.get("error_code") or "browser_failed")
            debug_dir = Path(str(result.get("debug_dir") or "")).name
            if debug_dir:
                logger.info(
                    "codex-rs recovery remote=...%s stage=browser_debug dir=%s code=%s",
                    _tail(remote_id), debug_dir, err_code,
                )
            result.clear()
            raise _Stop(err_code)
        session = oauth_sessions.get_session(ticket) or {}
        parsed = oauth_sessions.parse_oauth_callback(result.get("callback_url") or "")
        result.clear()
        if not parsed["code"] or parsed["error"] or not session or parsed["state"] != session.get("state"):
            raise _Stop("oauth_callback_invalid")
        _log(remote_id, "exchange")
        exchanged = await exchanger.exchange_oauth_code(parsed["code"], oauth_sessions.CLIENT_ID,
            oauth_sessions.REDIRECT_URI, session.get("code_verifier") or "", db, identifier=EXCHANGE_ID)
        await db.commit()
        if not exchanged.get("success"):
            raise _Stop("oauth_exchange_failed")
        identity = _token_identity(exchanged.get("access_token") or "") or {}
        email_before, user_before, workspace_before = _identity(before)
        if (not exchanged.get("refresh_token") or not email_before or identity.get("email") != email_before
                or not user_before or identity.get("user_id") != user_before
                or not workspace_before or identity.get("account_id") != workspace_before):
            raise _Stop("identity_mismatch")
        return {**holder, "email": identity["email"], "access_token": exchanged["access_token"],
                "refresh_token": exchanged["refresh_token"], "id_token": exchanged.get("id_token")}
    except BaseException:
        # The caller only learns ticket / exchanger from a successful return; clean up here otherwise.
        await exchanger.clear_session(EXCHANGE_ID)
        oauth_sessions.pop_session(ticket)
        raise


async def _restore_settings(client: CodexClient, remote_id: str, before: dict, proxy: str) -> str | None:
    """Put groups, proxy, concurrency and weight back if the import changed them; returns a note or None."""
    after = await client.detail(remote_id)
    old, new = _settings(before), _settings(after)
    if old == new:
        return None
    changes = {"groupIds": old["groupIds"], "concurrencyLimit": old["concurrencyLimit"], "weight": old["weight"]}
    if old["outboundProxyId"] != new["outboundProxyId"] or old["outboundProxyEndpoint"] != new["outboundProxyEndpoint"]:
        if old["outboundProxyId"]:
            changes["outboundProxyId"] = old["outboundProxyId"]
        else:
            changes["outboundProxyUrl"] = proxy
    try:
        await client.update_account(remote_id, after, **changes)
    except (CodexTransferError, ValueError):
        _log(remote_id, "restore_settings", "failed")
        raise _Stop("verify_failed") from None
    if _settings(await client.detail(remote_id)) != old:
        raise _Stop("verify_failed")
    _log(remote_id, "restore_settings", "restored")
    return "重新授权完成；写回后分组、代理或并发有变化，已改回原值"


# ---- scan ----------------------------------------------------------------------------------

def _due(entry: dict | None, remote: dict, now: datetime) -> bool:
    if not entry:
        return True
    if _running(entry):
        return False
    if entry.get("state") == "manual_required":
        # The user deleting the mark line re-arms the account; an unwritten mark never does.
        return bool(entry.get("marked")) and not _has_mark(remote.get("notes"))
    next_at = _parse_time(entry.get("next_at"))
    if next_at is not None and next_at > now:
        return False
    succeeded = _parse_time(entry.get("succeeded_at"))
    return succeeded is None or now - succeeded >= SUCCESS_COOLDOWN


async def _settle(db, client: CodexClient, items: list[dict], now: datetime) -> None:
    """Forget accounts that are no longer in error (or gone) and remove their mark lines."""
    data = await _load_state(db)
    await db.commit()
    by_id = {str(item.get("id")): item for item in items if item.get("id")}
    for remote_id, entry in list(data["accounts"].items()):
        remote = by_id.get(remote_id)
        if remote is not None and remote.get("status") == "error":
            continue
        succeeded = _parse_time(entry.get("succeeded_at"))
        if remote is not None and entry.get("state") == "succeeded" and succeeded and now - succeeded < SUCCESS_COOLDOWN:
            continue  # keep the cooldown until it runs out
        if entry.get("state") == "running" and _running(entry):
            continue
        await _put(db, remote_id, None)
        if remote is not None and _has_mark(remote.get("notes")):
            await _clear_mark(client, remote_id)


async def run_once(db) -> None:
    """Scheduler entry: at most one account per round, only while the switch is on."""
    if not await enabled(db) or not command_runner.ready or _LOCK.locked() or browser_lock().locked():
        await db.commit()
        return
    busy = await operation_store.browser_busy(db)
    await db.commit()
    if busy is not None:
        return
    client = await _client(db)
    if client is None:
        return
    try:
        items = await client.list_accounts()
    except CodexTransferError as exc:
        logger.warning("codex-rs recovery scan: list failed code=%s", exc.code)
        return
    now = utcnow()
    await _settle(db, client, items, now)
    data = await _load_state(db)
    data["last_scan_at"] = isoformat(now)
    await _save_state(db, data)
    candidates = sorted((item for item in items if isinstance(item.get("id"), str) and _is_candidate(item)),
                        key=lambda item: item["id"])
    legacy = await _legacy_ids(db, [item["id"] for item in candidates])
    for item in candidates:
        remote_id = item["id"]
        if remote_id in legacy:
            continue
        entry = data["accounts"].get(remote_id)
        if entry and entry.get("state") == "manual_required" and not entry.get("marked"):
            # Retry writing a mark line that failed earlier; never re-arm on its absence.
            if await _set_mark(client, remote_id, _manual_reason(entry)):
                await _put(db, remote_id, {**entry, "marked": True})
            continue
        if not _due(entry, item, now):
            continue
        if entry and entry.get("state") == "manual_required":
            await _put(db, remote_id, {**entry, "state": "retry_wait", "fail_count": 0, "next_at": None,
                                       "marked": False})
        await recover_account(db, remote_id)
        return


# ---- read-only views -----------------------------------------------------------------------

def _label(entry: dict) -> str:
    state = entry.get("state")
    code = entry.get("last_code")
    if state == "manual_required":
        return f"需人工：{_manual_reason(entry)}"
    if state == "retry_wait":
        if not code:
            return "等待处理"
        count = int(entry.get("fail_count") or 0)
        return f"等待重试：{_reason(code)}" + (f"（已失败 {count} 次）" if count else "")
    return STATE_LABELS.get(state, str(state or ""))


async def status(db) -> dict:
    data = await _load_state(db)
    is_on = await enabled(db)
    await db.commit()
    accounts = [{"remote_id": remote_id, "email": entry.get("email") or "", "state": entry.get("state"),
                 "label": _label(entry), "last_code": entry.get("last_code"), "updated_at": entry.get("updated_at")}
                for remote_id, entry in data["accounts"].items()]
    accounts.sort(key=lambda row: row["updated_at"] or "", reverse=True)
    return {"enabled": is_on, "last_scan_at": data.get("last_scan_at"), "accounts": accounts}


async def dry_run(db, remote_id: str) -> dict:
    """What recover_account would do, without refreshing, opening a browser or writing anything."""
    remote_id = str(remote_id or "").strip()
    report = {"remote_id": remote_id, "enabled": await enabled(db)}
    await db.commit()
    client = await _client(db)
    if client is None:
        return {**report, "error": _reason("codex_not_configured")}
    try:
        remote = await client.detail(remote_id)
    except CodexTransferError as exc:
        return {**report, "error": f"读取 rs 账号失败（{exc.code}）"}
    email = normalize_email(remote.get("email"))
    legacy = bool(await _legacy_ids(db, [remote_id]))
    candidate = _is_candidate(remote)
    report.update(status=remote.get("status"), error_reason=remote.get("errorReason"),
                  enabled_remote=remote.get("enabled"), candidate=candidate, legacy_binding=legacy,
                  would_refresh=candidate and not legacy, has_mark=_has_mark(remote.get("notes")),
                  state=(await _entry(db, remote_id)) or None)
    try:
        report["proxy"] = bool(await _proxy(client, remote_id))
    except _Stop:
        report["proxy"] = False
    except CodexTransferError as exc:
        report["proxy"] = f"读取失败（{exc.code}）"
    try:
        mailbox = await _find_mailbox(db, email)
        report["hme_account"] = "找到" if mailbox else "HME 暂时不可用"
    except _Stop as stop:
        report["hme_account"] = _reason(stop.code)
    team = await _team_name(db, remote.get("accountId"))
    report["team_name"] = team or None
    return report
