"""SMS relay for the signup extension (protocol: docs/contracts/phone-relay.md).

The extension fills the number and the code into the OpenAI page; the server owns the phone
pool lease, reads the SMS receipt URL through the account's proxy, and records the outcome.
Receipt URLs, full numbers, and codes never go into responses (except the number on acquire
and the new code on poll) or logs.

Callers pass an ``AsyncSession`` and handle routing; commits happen here (through
``phone_pool_service`` or directly).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.resources.phones import PhonePoolEmpty, phone_pool_service
from app.core.time import as_utc, utcnow
from app.domain.identity.ids import normalize_email
from app.domain.resources import (
    OUTCOME_CANCELLED,
    OUTCOME_INVALID,
    OUTCOME_NO_SMS,
    OUTCOME_PROVIDER_ERROR,
    OUTCOME_RECENTLY_USED,
    OUTCOME_RISK,
    OUTCOME_SUCCESS,
    normalize_phone_number,
)
from app.integrations.sms.client import SmsClient, require_proxy
from app.persistence.models.identity import Account, Workspace
from app.persistence.models.resources import PhonePool

logger = logging.getLogger(__name__)

SESSION_RE = re.compile(r"^[a-f0-9]{32}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SMS_FETCH_TIMEOUT = 10.0
# The local extension request times out at 30 s; the live team lookup must finish before that.
RESOLVE_TIMEOUT = 25.0
_PROXY_CACHE_TTL = 2 * 3600
_PROXY_CACHE_MAX = 500

PHASES = {"signup", "oauth"}
ACTIONS = {"acquire", "code", "report", "release"}
OUTCOMES = {
    OUTCOME_SUCCESS: OUTCOME_SUCCESS,
    OUTCOME_INVALID: OUTCOME_INVALID,
    OUTCOME_RECENTLY_USED: OUTCOME_RECENTLY_USED,
    OUTCOME_RISK: OUTCOME_RISK,
    OUTCOME_NO_SMS: OUTCOME_NO_SMS,
    "wrong_code": OUTCOME_PROVIDER_ERROR,
    OUTCOME_CANCELLED: OUTCOME_CANCELLED,
}
_OUTCOME_MESSAGES = {
    OUTCOME_INVALID: "插件上报：号码无效",
    OUTCOME_RECENTLY_USED: "插件上报：号码刚被用过",
    OUTCOME_RISK: "插件上报：短信发不出或被转到其他渠道",
    OUTCOME_NO_SMS: "插件上报：提交号码后 90 秒没收到短信",
    "wrong_code": "插件上报：短信验证码被拒",
    OUTCOME_CANCELLED: "插件上报：任务取消",
}

# (lease_key, phone_id) -> {"code": baseline code or None, "sms_url": receipt URL}; phone_id 0 = bound phone.
_BASELINES: dict[tuple[str, int], dict[str, Any]] = {}
# session -> (email, proxy_url, monotonic time); avoids a live team lookup on every local request.
_PROXY_CACHE: dict[str, tuple[str, str, float]] = {}


@dataclass(frozen=True)
class RelayContext:
    lease_key: str
    account_id: int | None
    proxy_url: str
    phase: str


def _fail(error_code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": error_code, "message": message}


def _lease_lost() -> dict[str, Any]:
    return _fail("lease_lost", "这个号码已不在本任务名下，请换号")


def _purpose(ctx: RelayContext) -> str:
    return "reauth" if ctx.phase == "oauth" else "signup"


def _proxy(value: str | None) -> str | None:
    try:
        return require_proxy(value, "接码")
    except ValueError:
        return None


def _display_number(raw: str) -> str:
    try:
        return normalize_phone_number(raw)
    except ValueError:
        return str(raw or "").strip()


def _drop_baselines(lease_key: str, phone_id: int | None = None) -> None:
    for key in [k for k in _BASELINES if k[0] == lease_key and (phone_id is None or k[1] == phone_id)]:
        _BASELINES.pop(key, None)


async def _read_sms(sms_url: str, proxy_url: str) -> str | None:
    """One read of the receipt URL; any error means "nothing yet"."""
    try:
        return await asyncio.to_thread(SmsClient(timeout=SMS_FETCH_TIMEOUT).fetch_code, sms_url, proxy=proxy_url)
    except Exception as exc:  # noqa: BLE001 - the extension's 90 s timer decides when to give up
        logger.debug("phone relay sms read failed: %s", type(exc).__name__)
        return None


async def _owned_row(db: AsyncSession, lease_key: str, phone_id: int, *, live: bool) -> PhonePool | None:
    """The pool row if this session still holds it; ``live`` also requires an unexpired lease."""
    if not phone_id or phone_id <= 0:
        return None
    row = await db.get(PhonePool, int(phone_id), populate_existing=True)
    if row is None or row.reserved_by != lease_key:
        return None
    if live:
        cfg = await phone_pool_service.get_config(db)
        # SQLite returns naive datetimes; compare in UTC.
        anchor = as_utc(row.lease_heartbeat_at or row.reserved_at)
        if anchor is not None and anchor < utcnow() - timedelta(seconds=cfg.reserve_sec):
            return None
    return row


async def _bound_phone(db: AsyncSession, ctx: RelayContext) -> tuple[str, str] | None:
    if not ctx.account_id:
        return None
    account = await db.get(Account, int(ctx.account_id), populate_existing=True)
    if account is None or not str(account.phone or "").strip() or not str(account.sms_url or "").startswith("http"):
        return None
    return str(account.phone).strip(), str(account.sms_url).strip()


async def acquire(db: AsyncSession, ctx: RelayContext, *, bound: bool) -> dict[str, Any]:
    proxy_url = _proxy(ctx.proxy_url)
    if not proxy_url:
        return _fail("proxy_unknown", "找不到这个邮箱所在团队的代理，无法接码")
    if bound:
        found = await _bound_phone(db, ctx)
        if found is None:
            return _fail("bound_phone_missing", "页面要求验证账号已绑定的手机号，但本地没有该账号的号码和接码链接")
        number, sms_url = found
        key = (ctx.lease_key, 0)
        if key not in _BASELINES:
            _BASELINES[key] = {"code": await _read_sms(sms_url, proxy_url), "sms_url": sms_url}
        return {"ok": True, "phoneId": 0, "number": _display_number(number), "bound": True}

    # A retried acquire (e.g. after a client timeout) gets the number this session already holds.
    held = await db.scalar(
        select(PhonePool)
        .where(PhonePool.reserved_by == ctx.lease_key)
        .order_by(PhonePool.reserved_at.desc(), PhonePool.id.desc())
    )
    row = await _owned_row(db, ctx.lease_key, held.id, live=True) if held is not None else None
    if row is not None:
        await phone_pool_service.heartbeat(db, ctx.lease_key)
    else:
        try:
            row = await phone_pool_service.acquire(db, ctx.lease_key)
        except PhonePoolEmpty:
            await db.rollback()
            return _fail("phone_pool_empty", "号码池没有可用号，去 Team48 资源页导入后点继续")
        except ValueError:
            await db.rollback()
            return _fail("invalid_request", "接码请求缺少任务标识")
    key = (ctx.lease_key, int(row.id))
    if key not in _BASELINES:
        _BASELINES[key] = {"code": await _read_sms(row.sms_url, proxy_url), "sms_url": row.sms_url}
    return {"ok": True, "phoneId": int(row.id), "number": row.number, "bound": False}


async def poll_code(db: AsyncSession, ctx: RelayContext, *, phone_id: int, bound: bool) -> dict[str, Any]:
    if bound:
        baseline = _BASELINES.get((ctx.lease_key, 0))
        if baseline is None:
            return _lease_lost()
        sms_url = baseline["sms_url"]
    else:
        row = await _owned_row(db, ctx.lease_key, phone_id, live=True)
        if row is None:
            _drop_baselines(ctx.lease_key, int(phone_id or 0))
            return _lease_lost()
        sms_url = row.sms_url
        await phone_pool_service.heartbeat(db, ctx.lease_key)
        # After a server restart the baseline is gone; then any code counts as new.
        baseline = _BASELINES.setdefault((ctx.lease_key, int(row.id)), {"code": None, "sms_url": sms_url})
    proxy_url = _proxy(ctx.proxy_url)
    code = await _read_sms(sms_url, proxy_url) if proxy_url else None
    if not code or code == baseline.get("code"):
        return {"ok": True, "code": None}
    return {"ok": True, "code": code}


async def report(db: AsyncSession, ctx: RelayContext, *, phone_id: int, bound: bool, outcome: str) -> dict[str, Any]:
    wanted = str(outcome or "").strip().lower()
    if wanted not in OUTCOMES:
        return _fail("invalid_request", "未知的接码结果")
    if bound:
        _drop_baselines(ctx.lease_key, 0)
        return {"ok": True}
    # Ownership only: a lease past its heartbeat but not yet reclaimed still belongs to this session.
    row = await _owned_row(db, ctx.lease_key, phone_id, live=False)
    if row is None:
        _drop_baselines(ctx.lease_key, int(phone_id or 0))
        return _lease_lost()
    number, sms_url = row.number, row.sms_url
    await phone_pool_service.record_result(
        db,
        result=OUTCOMES[wanted],
        job_id=ctx.lease_key,
        phone_id=int(row.id),
        message=_OUTCOME_MESSAGES.get(wanted, ""),
        purpose=_purpose(ctx),
        account_id=ctx.account_id,
    )
    if wanted == OUTCOME_SUCCESS and ctx.account_id:
        account = await db.get(Account, int(ctx.account_id))
        if account is not None:
            account.phone = number
            account.sms_url = sms_url
            account.updated_at = utcnow()
            await db.commit()
    _drop_baselines(ctx.lease_key, int(row.id))
    return {"ok": True}


async def release(db: AsyncSession, ctx: RelayContext) -> dict[str, Any]:
    rows = list((await db.scalars(select(PhonePool).where(PhonePool.reserved_by == ctx.lease_key))).all())
    for row in rows:
        phone_pool_service._clear_lease(row)
        row.updated_at = utcnow()
    if rows:
        await db.commit()
    _drop_baselines(ctx.lease_key)
    return {"ok": True}


async def handle(
    db: AsyncSession,
    ctx: RelayContext,
    *,
    action: str,
    phone_id: int | None = None,
    bound: bool = False,
    outcome: str | None = None,
) -> dict[str, Any]:
    """Dispatch one validated request body to the matching action."""
    if action == "acquire":
        return await acquire(db, ctx, bound=bound)
    if action == "code":
        return await poll_code(db, ctx, phone_id=int(phone_id or 0), bound=bound)
    if action == "report":
        return await report(db, ctx, phone_id=int(phone_id or 0), bound=bound, outcome=str(outcome or ""))
    if action == "release":
        return await release(db, ctx)
    return _fail("invalid_request", "未知的接码动作")


def _cached_proxy(session: str, email: str) -> str | None:
    hit = _PROXY_CACHE.get(session)
    if hit is None or hit[0] != email or time.monotonic() - hit[2] > _PROXY_CACHE_TTL:
        return None
    return hit[1]


def _remember_proxy(session: str, email: str, proxy_url: str) -> None:
    now = time.monotonic()
    if len(_PROXY_CACHE) >= _PROXY_CACHE_MAX:
        for key in [k for k, v in _PROXY_CACHE.items() if now - v[2] > _PROXY_CACHE_TTL]:
            _PROXY_CACHE.pop(key, None)
        while len(_PROXY_CACHE) >= _PROXY_CACHE_MAX:
            _PROXY_CACHE.pop(next(iter(_PROXY_CACHE)))
    _PROXY_CACHE[session] = (email, proxy_url, now)


async def _owner_proxy(db: AsyncSession, workspace_id: int) -> str | None:
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None or not workspace.owner_account_id:
        return None
    # Async sessions cannot lazy-load workspace.owner_account.
    owner = await db.get(Account, workspace.owner_account_id)
    return _proxy(owner.proxy) if owner is not None else None


async def personal_context(
    db: AsyncSession,
    *,
    session: str,
    email: str,
    workspace_id: int | None,
    phase: str,
) -> RelayContext | dict[str, Any]:
    """Build the context for a local-extension request, or return the error body."""
    key = str(session or "").strip().lower()
    target = normalize_email(email)
    if not SESSION_RE.fullmatch(key):
        return _fail("invalid_request", "接码会话标识不正确")
    if not _EMAIL_RE.fullmatch(target):
        return _fail("invalid_request", "邮箱格式不正确")
    account = await db.scalar(select(Account).where(Account.email == target))
    account_id = int(account.id) if account is not None else None

    proxy_url = _proxy(account.proxy) if account is not None else None
    if not proxy_url and workspace_id:
        proxy_url = await _owner_proxy(db, workspace_id)
    if not proxy_url:
        proxy_url = _cached_proxy(key, target)
    if not proxy_url:
        from app.application.member_handoff import resolve_extension_workspace

        try:
            resolved = await asyncio.wait_for(resolve_extension_workspace(db, email=target), timeout=RESOLVE_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 - lookup timeout or team read failure
            logger.debug("phone relay workspace lookup failed: %s", type(exc).__name__)
            await db.rollback()
            resolved = {}
        if resolved.get("ok") and resolved.get("state") == "found":
            proxy_url = await _owner_proxy(db, int(resolved["workspace"]["id"]))
    if not proxy_url:
        return _fail("proxy_unknown", "找不到这个邮箱所在团队的代理，无法接码")
    _remember_proxy(key, target, proxy_url)
    return RelayContext(
        lease_key=key,
        account_id=account_id,
        proxy_url=proxy_url,
        phase=phase if phase in PHASES else "signup",
    )
