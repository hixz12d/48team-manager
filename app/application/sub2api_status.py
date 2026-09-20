"""Frequent, read-only Sub2API inventory checks; never mutate remote accounts.

Presence is confirmed only from a complete, validated Admin account listing.
Billing, official quota, local authorization and credential-sync receipts remain
independent. Network/authentication failures preserve observations as stale.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.sqlite import insert

from app.core.time import as_utc, isoformat, utcnow
from app.domain.identity.binding import remote_snapshot
from app.domain.identity.ids import normalize_email
from app.integrations.sub2api.client import sub2api_client
from app.persistence.models.identity import Account, ExternalBinding
from app.persistence.models.sub2api_status import Sub2ApiAccountStatus

REFRESH_SECONDS = 15
STALE_SECONDS = 45
_LOCK = asyncio.Lock()
LABELS = {
    "healthy": ("远端正常", "success"),
    "missing": ("远端已删除 / 不存在", "error"),
    "paused": ("远端已暂停", "muted"),
    "auth_error": ("远端授权失效", "error"),
    "phone_required": ("远端需要手机验证", "warning"),
    "forbidden": ("远端访问被拒绝", "warning"),
    "rate_limited": ("远端限流 / 限额", "warning"),
    "temporary_hold": ("远端临时不可调度", "warning"),
    "error": ("远端异常", "error"),
    "identity_mismatch": ("远端身份不一致", "error"),
    "identity_unconfirmed": ("远端身份待核对", "warning"),
    "binding_review": ("绑定待核对", "warning"),
    "unknown": ("远端状态未确认", "muted"),
    "unbound": ("未绑定 Sub2API", "muted"),
}
ERRORS = {
    "not_configured": "Sub2API 尚未配置",
    "admin_auth_failed": "Sub2API 管理认证失败",
    "unavailable": "暂时无法核对 Sub2API",
    "invalid_inventory": "远端列表不完整或格式异常",
    "connection_changed": "Sub2API 连接已变化，等待重新核对",
}


def source_signature(config):
    return hashlib.sha256(str(config.get("base_url") or "").rstrip("/").encode()).hexdigest()


def binding_signature(binding, account):
    values = [binding.id, binding.local_account_id, binding.workspace_id, binding.remote_account_id,
              binding.binding_state, binding.verified_email, binding.verified_workspace_id,
              binding.verified_official_account_id, account.email, account.official_account_id]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


def _future(value):
    try:
        return as_utc(datetime.fromisoformat(str(value).replace("Z", "+00:00"))) > utcnow()
    except (TypeError, ValueError):
        return False


def classify(remote):
    if remote.get("status") in {"inactive", "disabled"}:
        return "paused"
    if remote.get("status") == "error":
        kind = sub2api_client.schedule_kind(remote)["kind"]
        return {"phone": "phone_required", "401": "auth_error", "403": "forbidden"}.get(kind, "error")
    if remote.get("schedulable") is False:
        return "paused"
    # Old usage percentages and error text are not current runtime status.
    if _future(remote.get("rate_limit_reset_at")):
        return "rate_limited"
    if any(_future(remote.get(key)) for key in ("temp_unschedulable_until", "overload_until")):
        return "temporary_hold"
    return "healthy" if remote.get("status") == "active" and remote.get("schedulable") is True else "unknown"


def observe(binding, account, remote):
    if remote is None:
        return {"exists": False, "state": "missing", "schedulable": None}
    identity = remote_snapshot(remote)
    expected_email = normalize_email(account.email)
    expected_official = binding.verified_official_account_id or account.official_account_id
    mismatched = (
        bool(identity["email"] and normalize_email(identity["email"]) != expected_email)
        or bool(expected_official and identity["official_account_id"] and identity["official_account_id"] != expected_official)
        or bool(binding.verified_workspace_id and identity["workspace_id"] and identity["workspace_id"] != binding.verified_workspace_id)
    )
    state = "identity_mismatch" if mismatched else (classify(remote) if identity["email"] else "identity_unconfirmed")
    return {"exists": True, "state": state,
            "schedulable": remote.get("schedulable") if type(remote.get("schedulable")) is bool else None}


async def _bindings(db):
    return list((await db.execute(select(ExternalBinding, Account).join(Account, Account.id == ExternalBinding.local_account_id)
        .where(ExternalBinding.provider == "sub2api").execution_options(populate_existing=True))).all())


def _matches(row, binding, account, source):
    return bool(row and row.source_signature == source and row.binding_signature == binding_signature(binding, account))


def present(row=None, *, binding=None, account=None, source=None, configured=True):
    state = "unbound" if binding is None else "unknown"
    result = {"state": state, "label": LABELS[state][0], "severity": LABELS[state][1], "exists": None,
              "checked_at": None, "last_attempt_at": None, "stale": True, "schedulable": None,
              "remote_id": str(binding.remote_account_id) if binding else None}
    if binding is None:
        return result
    if binding.binding_state not in {"verified", "missing"}:
        return {**result, "state": "binding_review", "label": LABELS["binding_review"][0], "severity": "warning"}
    if not configured or not _matches(row, binding, account, source):
        return {**result, "message": ERRORS["not_configured"] if not configured else "等待核对远端账号是否存在"}
    stale = bool(row.last_error_code or not row.checked_at or utcnow() - as_utc(row.checked_at) > timedelta(seconds=STALE_SECONDS))
    state = row.state if row.state in LABELS else "unknown"
    label, severity = LABELS[state]
    result.update(state=state, label=label, severity=severity, exists=row.exists, stale=stale,
                  checked_at=isoformat(row.checked_at), last_attempt_at=isoformat(row.last_attempt_at), schedulable=row.schedulable)
    if row.last_error_code:
        result.update(state="unknown", label=ERRORS.get(row.last_error_code, ERRORS["unavailable"]), severity="warning",
                      exists=None, last_known_state=state, last_known_label=label, error_code=row.last_error_code)
    return result


async def payloads(db):
    """Local snapshots only; safe for frequently polled portfolio reads."""
    config = await sub2api_client.load_config(db)
    source = source_signature(config)
    bindings = await _bindings(db)
    rows = {row.binding_id: row for row in (await db.scalars(select(Sub2ApiAccountStatus).execution_options(populate_existing=True))).all()}
    by_context = {}
    items = []
    for binding, account in bindings:
        item = present(rows.get(binding.id), binding=binding, account=account, source=source, configured=config.get("configured", False))
        key = (account.id, binding.workspace_id)
        if key in by_context:
            item = {**item, "state": "binding_review", "label": "存在多个远端绑定", "severity": "warning", "exists": None}
        by_context[key] = item
        items.append(item)
    checked = [item["checked_at"] for item in items if item["checked_at"]]
    summary = {"configured": bool(config.get("configured")), "bindings": len(items),
               "checked_at": min(checked) if checked else None, "stale": any(item["stale"] for item in items),
               "missing": sum(item["state"] == "missing" for item in items),
               "refresh_seconds": REFRESH_SECONDS}
    return by_context, summary


async def record_readback(db, binding, account, remote):
    """Publish a verified write's readback immediately; older inventory reads cannot overwrite it."""
    if not isinstance(remote, dict) or str(remote.get("id")) != str(binding.remote_account_id):
        return
    config = await sub2api_client.load_config(db)
    stamp = utcnow()
    values = dict(binding_id=binding.id, remote_account_id=str(binding.remote_account_id),
                  source_signature=source_signature(config), binding_signature=binding_signature(binding, account),
                  last_attempt_at=stamp, checked_at=stamp, last_error_code=None,
                  **observe(binding, account, remote))
    await db.execute(insert(Sub2ApiAccountStatus).values(**values).on_conflict_do_update(
        index_elements=["binding_id"], set_={k: v for k, v in values.items() if k != "binding_id"},
        where=Sub2ApiAccountStatus.last_attempt_at <= stamp))


async def refresh(db, *, force=False):
    """Coalesce requests and update only local observations, not bindings or tokens."""
    if _LOCK.locked():
        return {"ok": True, "refreshing": True, "message": "正在核对 Sub2API"}
    async with _LOCK:
        config = await sub2api_client.load_config(db)
        if not config.get("configured"):
            return {"ok": False, "error_code": "not_configured", "message": ERRORS["not_configured"]}
        source = source_signature(config)
        initial = await _bindings(db)
        if not initial:
            return {"ok": True, "checked": 0, "message": "没有需要核对的远端绑定"}
        rows = {row.binding_id: row for row in (await db.scalars(select(Sub2ApiAccountStatus).execution_options(populate_existing=True))).all()}
        started = utcnow()
        signatures = {b.id: binding_signature(b, a) for b, a in initial}
        if not force and all(_matches(rows.get(b.id), b, a, source)
            and as_utc(rows[b.id].last_attempt_at) > started - timedelta(seconds=REFRESH_SECONDS) for b, a in initial):
            return {"ok": True, "cached": True, "message": "使用刚核对的 Sub2API 状态"}
        # End the read transaction before waiting on the remote server.
        await db.commit()
        code = None
        inventory = {}
        try:
            async with asyncio.timeout(20):
                remotes = await sub2api_client.list_status_accounts(db, config)
            if not isinstance(remotes, list):
                raise ValueError("invalid inventory")
            for remote in remotes:
                if not isinstance(remote, dict) or not str(remote.get("id", "")).isdigit() or int(remote["id"]) < 1:
                    raise ValueError("invalid remote id")
                key = str(int(remote["id"]))
                if key in inventory:
                    raise ValueError("duplicate remote id")
                inventory[key] = remote
        except httpx.HTTPStatusError as exc:
            code = "admin_auth_failed" if exc.response.status_code in {401, 403} else "unavailable"
        except (httpx.HTTPError, TimeoutError):
            code = "unavailable"
        except (ValueError, RuntimeError):
            code = "invalid_inventory"
        if source_signature(await sub2api_client.load_config(db)) != source:
            return {"ok": False, "error_code": "connection_changed", "message": ERRORS["connection_changed"]}
        current = await _bindings(db)
        checked = 0
        for binding, account in current:
            if signatures.get(binding.id) != binding_signature(binding, account):
                continue
            values = dict(binding_id=binding.id, remote_account_id=str(binding.remote_account_id), source_signature=source,
                          binding_signature=signatures[binding.id], last_attempt_at=started, last_error_code=code)
            old = rows.get(binding.id)
            if code:
                if not _matches(old, binding, account, source):
                    values.update(exists=None, state="unknown", schedulable=None, checked_at=None)
            elif binding.binding_state not in {"verified", "missing"} or not str(binding.remote_account_id).isdigit() or int(binding.remote_account_id) < 1:
                values.update(exists=None, state="binding_review", schedulable=None, checked_at=started)
            else:
                values.update(observe(binding, account, inventory.get(str(int(binding.remote_account_id)))))
                values["checked_at"] = started
            await db.execute(insert(Sub2ApiAccountStatus).values(**values).on_conflict_do_update(
                index_elements=["binding_id"], set_={k: v for k, v in values.items() if k != "binding_id"},
                where=Sub2ApiAccountStatus.last_attempt_at <= started))
            checked += 1
        await db.commit()
        return {"ok": code is None, "checked": checked, "error_code": code,
                "message": ERRORS[code] if code else f"已核对 {checked} 个 Sub2API 绑定"}
