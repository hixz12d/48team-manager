"""codex-rs auto refill: keep enough working accounts in codex-rs by pulling from the standby pool.

Config (``codex_rs_refill``) and runtime state (``codex_rs_refill_state``) both live in
``system_settings`` as JSON. Only this module reads or writes the state key.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.codex_export import CodexTransferError
from app.application.mailbox import mailbox_readiness_snapshot
from app.application.settings import get_setting_value, upsert_setting
from app.application.workspace_expiry import EXPIRY_TIMEZONE
from app.application.workspace_switch_count import SWITCH_TIMEZONE, switch_count_record
from app.core.time import as_utc, utcnow, zone
from app.domain.automation import ACTIVE_STATES, WORKSPACE_LOCK_ACTIONS
from app.domain.identity import MEMBERSHIP_STATE_JOINED
from app.persistence.models.codex import CodexBinding
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership
from app.persistence.models.pool import StandbyPoolEntry

logger = logging.getLogger(__name__)

CONFIG_KEY = "codex_rs_refill"
STATE_KEY = "codex_rs_refill_state"
DEFAULT_ENABLED = False
DEFAULT_TARGET = 6
DEFAULT_DAILY_LIMIT = 6
MAX_WORKSPACE_IDS = 500
STALE_MINUTES = 15
MAX_DISABLED_RECORDS = 20
# Error / disabled accounts must stay that way across two scans before they stop counting.
DEBOUNCE_SECONDS = 4 * 60
WEEK_WINDOW_SECONDS = 604800
MAX_FAILURES = 2
MAX_TEAM_TRIES = 3
MAX_ENTRY_TRIES = 3
SOURCE = "codex_refill"
TEAM_FAILURE_CODES = {"team_full", "rotation_unresolved", "operation_conflict", "not_found"}
READ_FAILED = "codex-rs 读取失败"


def default_config() -> dict[str, Any]:
    return {"enabled": DEFAULT_ENABLED, "target": DEFAULT_TARGET, "daily_limit": DEFAULT_DAILY_LIMIT, "workspace_ids": []}


def default_state() -> dict[str, Any]:
    return {
        "checked_at": None, "ok": None, "error": None,
        "working": None, "total": None,
        "not_working": [],
        "suspects": {},
        "disabled_by_refill": [],
        "paused": False, "pause_reason": None, "consecutive_failures": 0,
        "last_operation_id": None, "last_counted_operation_id": None, "last_result": None,
        "today": {"date": None, "count": 0},
        "blocked_reason": None,
    }


def _load_json(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        value = {}
    return value if isinstance(value, dict) else {}


def _today() -> str:
    return utcnow().astimezone(zone(SWITCH_TIMEZONE)).date().isoformat()


async def load_config(db: AsyncSession) -> dict[str, Any]:
    """Malformed stored values fall back to defaults field by field."""
    result = default_config()
    stored = _load_json(await get_setting_value(db, CONFIG_KEY))
    if isinstance(stored.get("enabled"), bool):
        result["enabled"] = stored["enabled"]
    target = stored.get("target")
    if type(target) is int and 1 <= target <= 50:
        result["target"] = target
    limit = stored.get("daily_limit")
    if type(limit) is int and 0 <= limit <= 50:
        result["daily_limit"] = limit
    ids = stored.get("workspace_ids")
    if isinstance(ids, list):
        result["workspace_ids"] = sorted({value for value in ids if type(value) is int and value > 0})[:MAX_WORKSPACE_IDS]
    return result


async def save_config(db: AsyncSession, config: dict[str, Any]) -> None:
    """Overwrite the whole config block; caller commits."""
    await upsert_setting(db, CONFIG_KEY, json.dumps(config), "codex-rs 自动补号设置")


async def load_state(db: AsyncSession) -> dict[str, Any]:
    """Missing or malformed fields are filled with defaults."""
    state = default_state()
    stored = _load_json(await get_setting_value(db, STATE_KEY))
    for key, default in state.items():
        if key not in stored:
            continue
        value = stored[key]
        if isinstance(default, bool):
            if isinstance(value, bool):
                state[key] = value
        elif isinstance(default, int):
            if type(value) is int and value >= 0:
                state[key] = value
        elif isinstance(default, list):
            if isinstance(value, list):
                state[key] = [item for item in value if isinstance(item, dict)]
        elif isinstance(default, dict):
            if isinstance(value, dict):
                state[key] = value
        else:
            state[key] = value
    state["disabled_by_refill"] = state["disabled_by_refill"][-MAX_DISABLED_RECORDS:]
    today = state["today"] if isinstance(state["today"], dict) else {}
    count = today.get("count")
    state["today"] = {
        "date": today.get("date") if isinstance(today.get("date"), str) else None,
        "count": count if type(count) is int and count >= 0 else 0,
    }
    return state


async def save_state(db: AsyncSession, state: dict[str, Any]) -> None:
    """Upsert only; the caller commits."""
    state = dict(state)
    state["disabled_by_refill"] = list(state.get("disabled_by_refill") or [])[-MAX_DISABLED_RECORDS:]
    await upsert_setting(db, STATE_KEY, json.dumps(state, ensure_ascii=False), "codex-rs 自动补号运行状态")


async def pool_available(db: AsyncSession) -> int:
    """Pending standby entries whose mailbox is readable. Local DB only."""
    rows = (
        await db.execute(
            select(Account)
            .join(StandbyPoolEntry, StandbyPoolEntry.account_id == Account.id)
            .where(StandbyPoolEntry.state == "pending")
        )
    ).scalars().all()
    return sum(1 for account in rows if mailbox_readiness_snapshot(account)["ready"])


def _is_stale(config: dict[str, Any], state: dict[str, Any]) -> bool:
    if not config["enabled"]:
        return False
    raw = state.get("checked_at")
    if not raw:
        return True
    try:
        checked = as_utc(datetime.fromisoformat(str(raw).replace("Z", "+00:00")))
    except ValueError:
        return True
    return utcnow() - checked > timedelta(minutes=STALE_MINUTES)


def _public_state(state: dict[str, Any]) -> dict[str, Any]:
    view = {key: value for key, value in state.items() if key != "suspects"}
    today = _today()
    if view["today"].get("date") != today:
        view["today"] = {"date": today, "count": 0}
    return view


async def status(db: AsyncSession) -> dict[str, Any]:
    config = await load_config(db)
    state = await load_state(db)
    return {
        "config": config,
        "state": _public_state(state),
        "pool_available": await pool_available(db),
        "stale": _is_stale(config, state),
    }


async def overview(db: AsyncSession) -> dict[str, Any]:
    """Overview summary; local DB only, never calls codex-rs."""
    config = await load_config(db)
    state = await load_state(db)
    return {
        "enabled": config["enabled"],
        "working": state["working"],
        "target": config["target"],
        "pool_available": await pool_available(db),
        "paused": state["paused"],
        "pause_reason": state["pause_reason"],
        "blocked_reason": state["blocked_reason"],
        "error": state["error"],
        "stale": _is_stale(config, state),
    }


async def clear_pause(db: AsyncSession) -> None:
    """Reset the failure pause; caller commits."""
    state = await load_state(db)
    state.update(paused=False, pause_reason=None, consecutive_failures=0)
    await save_state(db, state)


async def resume(db: AsyncSession) -> dict[str, Any]:
    await clear_pause(db)
    await db.commit()
    return await status(db)


async def run_once(db: AsyncSession) -> dict[str, Any]:
    """One scheduled scan: count working codex-rs accounts, disable errored ones, refill if short.

    Never raises; any unexpected error is recorded as ``ok=false`` in the state.
    """
    try:
        config = await load_config(db)
        if not config["enabled"]:
            await db.commit()
            return {"ran": False}
        return await _run(db, config)
    except Exception as exc:  # noqa: BLE001 - the scheduler must keep running
        logger.exception("codex-rs auto refill failed")
        try:
            await db.rollback()
            state = await load_state(db)
            state.update(checked_at=utcnow().isoformat(), ok=False, error=f"自动补号检查出错：{type(exc).__name__}")
            await save_state(db, state)
            await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("codex-rs auto refill: could not record failure")
            await db.rollback()
        return {"ran": True, "ok": False}


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return as_utc(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    except ValueError:
        return None


async def _codex_client(db: AsyncSession):
    from app.integrations.codex.client import CodexClient, load_config as load_codex_config

    try:
        config = await load_codex_config(db)
    except ValueError:
        raise CodexTransferError("codex_not_configured", "已保存的 codex-rs 地址无效，请重新保存") from None
    return CodexClient(config["base_url"], config["api_key"])


async def _run(db: AsyncSession, config: dict[str, Any]) -> dict[str, Any]:
    now = utcnow()
    stamp = now.isoformat()
    state = await load_state(db)
    was_paused = state["paused"]
    today = _today()
    if state["today"].get("date") != today:
        state["today"] = {"date": today, "count": 0}

    try:
        client = await _codex_client(db)
        await db.commit()  # never hold a read transaction across network I/O
        items = await client.list_accounts()
    except Exception as exc:  # noqa: BLE001 - a failed read never counts as zero accounts
        await db.rollback()
        error = str(exc) if isinstance(exc, CodexTransferError) else READ_FAILED
        logger.warning("codex-rs auto refill: account list failed error=%s", getattr(exc, "code", type(exc).__name__))
        state.update(checked_at=stamp, ok=False, error=error, blocked_reason=READ_FAILED)
        await _save(db, state, was_paused)
        return {"ran": True, "ok": False, "error": error}

    working, not_working, to_disable = _classify(items, state, now)
    state.update(checked_at=stamp, ok=True, error=None, working=working, total=len(items), not_working=not_working)

    for item in to_disable:
        state["disabled_by_refill"].append(await _disable(db, client, item))
        logger.info("codex-rs auto refill: disabled errored account email=%s ok=%s",
                    item.get("email"), state["disabled_by_refill"][-1]["ok"])

    in_flight = await _settle_last_operation(db, state)

    launched = None
    if working >= config["target"]:
        state["blocked_reason"] = None
    elif state["paused"]:
        state["blocked_reason"] = "自动补号已暂停"
    elif in_flight:
        state["blocked_reason"] = "上一个补号任务还在进行"
    else:
        launched, state["blocked_reason"] = await _refill(db, client, config, state)
        if launched:
            state["last_operation_id"] = launched
            state["today"]["count"] += 1
            logger.info("codex-rs auto refill: launched pool_join operation_id=%s", launched)
    await _save(db, state, was_paused)
    return {"ran": True, "ok": True, "working": working, "total": len(items),
            "launched": launched, "blocked_reason": state["blocked_reason"]}


async def _save(db: AsyncSession, state: dict[str, Any], was_paused: bool) -> None:
    await db.rollback()  # drop anything a failed step left behind; the state dict is the source of truth
    if was_paused:
        fresh = await load_state(db)
        if not fresh["paused"]:
            # Resumed while this scan ran: the operator's resume wins.
            state.update(paused=False, pause_reason=None, consecutive_failures=fresh["consecutive_failures"])
    await save_state(db, state)
    await db.commit()


def _weekly_exhausted(item: dict[str, Any]) -> bool:
    """True only when a window of about 7 days or longer is used up (codex-rs allows 5% slack on the week)."""
    quota = item.get("quota")
    windows = quota.get("windows") if isinstance(quota, dict) else None
    if not isinstance(windows, list):
        return False
    for window in windows:
        if not isinstance(window, dict):
            continue
        seconds = window.get("windowSeconds")
        if type(seconds) not in (int, float) or seconds < WEEK_WINDOW_SECONDS - WEEK_WINDOW_SECONDS // 20:
            continue
        used = window.get("usedPercent")
        if window.get("limitReached") is True or (type(used) in (int, float) and used >= 100):
            return True
    return False


def _classify(items: list[dict[str, Any]], state: dict[str, Any], now: datetime) -> tuple[int, list, list]:
    """Return (working count, not_working records, errored accounts to disable); updates suspects in place."""
    suspects = {key: value for key, value in state["suspects"].items() if isinstance(key, str)}
    previous_since = {(item.get("remote_id"), item.get("reason")): item.get("since") for item in state["not_working"]}
    seen: set[str] = set()
    working = 0
    not_working: list[dict[str, Any]] = []
    to_disable: list[dict[str, Any]] = []
    for item in items:
        remote_id = str(item.get("id") or "")
        status = str(item.get("status") or "")
        enabled = item.get("enabled")
        email = item.get("email") if isinstance(item.get("email"), str) else None
        seen.add(remote_id)
        if enabled is False or status == "disabled":
            reason = "disabled"
        elif status == "error":
            reason = "error"
        elif status == "quota_exhausted" and _weekly_exhausted(item):
            reason = "weekly_exhausted"
        else:
            # normal, rate_limited, 5-hour quota used up: still counts.
            suspects.pop(remote_id, None)
            working += 1
            continue
        record = {"remote_id": remote_id, "email": email, "status": status, "reason": reason}
        if reason == "weekly_exhausted":
            suspects.pop(remote_id, None)
            not_working.append({**record, "since": previous_since.get((remote_id, reason)) or now.isoformat()})
            continue
        first = _parse_time(suspects.get(remote_id))
        if first is None:
            suspects[remote_id] = now.isoformat()
            working += 1
            continue
        if (now - first).total_seconds() < DEBOUNCE_SECONDS:
            working += 1
            continue
        not_working.append({**record, "since": suspects[remote_id]})
        if reason == "error" and enabled is True and remote_id:
            to_disable.append(record)
    state["suspects"] = {key: value for key, value in suspects.items() if key in seen}
    return working, not_working, to_disable


async def _disable(db: AsyncSession, client, item: dict[str, Any]) -> dict[str, Any]:
    """Turn scheduling off for one errored account and confirm it. Never deletes, never kicks."""
    remote_id = item["remote_id"]
    record = {"remote_id": remote_id, "email": item.get("email"), "at": utcnow().isoformat(), "ok": False, "error": None}
    binding_values: dict[str, Any]
    try:
        await client.set_enabled(remote_id, False)
        enabled = (await client.detail(remote_id)).get("enabled")
        if enabled is False:
            record["ok"] = True
            binding_values = {"remote_enabled": False, "last_error": None}
        else:
            record["error"] = "codex-rs 未确认停用，请到 codex-rs 后台核对"
            binding_values = {"remote_enabled": enabled if isinstance(enabled, bool) else None,
                              "last_error": "codex_rs_disable_unconfirmed"}
    except Exception as exc:  # noqa: BLE001 - a failed disable never blocks the refill
        code = exc.code if isinstance(exc, CodexTransferError) else "codex_rs_disable_failed"
        record["error"] = f"codex-rs 停用失败：{exc}" if isinstance(exc, CodexTransferError) else "codex-rs 停用失败"
        binding_values = {"last_error": code[:80]}
    try:
        await db.execute(update(CodexBinding).where(CodexBinding.remote_account_id == remote_id)
                         .values(**binding_values).execution_options(synchronize_session=False))
        await db.commit()
    except Exception:  # noqa: BLE001
        logger.exception("codex-rs auto refill: binding update failed remote_id=%s", remote_id)
        await db.rollback()
    return record


def _operation_outcome(op) -> tuple[bool, str]:
    if op is None:
        return False, "补号任务记录不存在"
    email = op.email or "补号任务"
    if op.state == "success":
        try:
            result = json.loads(op.result_json) if op.result_json else {}
        except (TypeError, ValueError):
            result = {}
        followups = result.get("followups") if isinstance(result, dict) else None
        codex = followups.get("codex_rs") if isinstance(followups, dict) else None
        if isinstance(codex, dict) and codex.get("ok") is True:
            return True, f"{email} 已入组并导入 codex-rs"
        detail = codex.get("message") if isinstance(codex, dict) else None
        return False, f"{email} 已入组，但导入 codex-rs 失败：{detail or '结果未知'}"
    label = "待人工处理" if op.state in {"manual_required", "partial"} else "拉入失败"
    return False, f"{email} {label}：{op.error_message or op.state}"


async def _settle_last_operation(db: AsyncSession, state: dict[str, Any]) -> bool:
    """Count the previous refill's result once. Returns True while it is still running."""
    from app.application.operations import operation_store

    public_id = state["last_operation_id"]
    if not public_id:
        return False
    op = await operation_store.get_by_public_id(db, str(public_id))
    if op is not None and op.state in ACTIVE_STATES:
        return True
    if state["last_counted_operation_id"] == public_id:
        return False
    ok, message = _operation_outcome(op)
    state["last_result"] = message
    state["last_counted_operation_id"] = public_id
    if ok:
        state["consecutive_failures"] = 0
    else:
        state["consecutive_failures"] += 1
        logger.warning("codex-rs auto refill: operation %s did not finish cleanly", public_id)
        if state["consecutive_failures"] >= MAX_FAILURES and not state["paused"]:
            state["paused"] = True
            state["pause_reason"] = f"连续 {MAX_FAILURES} 次补号失败：{message}"
    return False


def _expired(workspace: Workspace) -> bool:
    if not workspace.manual_expires_on:
        return False
    return workspace.manual_expires_on < utcnow().astimezone(zone(EXPIRY_TIMEZONE)).date()


async def _candidate_workspaces(db: AsyncSession, workspace_ids: list[int]) -> list[int]:
    from app.application.manual_rotation import unresolved_for_workspace
    from app.application.operations import operation_store

    rows = (await db.scalars(select(Workspace).where(Workspace.id.in_(workspace_ids)))).all()
    ranked: list[tuple[int, int]] = []
    for workspace in rows:
        if workspace.status != "active" or not workspace.official_workspace_id or _expired(workspace):
            continue
        owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
        if owner is None or not owner.proxy:
            continue
        # Unknown seat counts do not exclude: the operator picked these teams.
        if workspace.seat_limit is not None and workspace.occupied_seats is not None \
                and workspace.occupied_seats >= workspace.seat_limit:
            continue
        if await operation_store.active_for_workspace(db, workspace.id, actions=WORKSPACE_LOCK_ACTIONS) is not None:
            continue
        if await unresolved_for_workspace(db, workspace.id) is not None:
            continue
        ranked.append((int(switch_count_record(workspace)["count"]), workspace.id))
    return [workspace_id for _, workspace_id in sorted(ranked)]


async def _candidate_entries(db: AsyncSession, limit: int) -> list[tuple[int, str]]:
    rows = (await db.execute(
        select(StandbyPoolEntry.id, Account)
        .join(Account, Account.id == StandbyPoolEntry.account_id)
        .where(StandbyPoolEntry.state == "pending")
        .order_by(StandbyPoolEntry.imported_at, StandbyPoolEntry.id)
    )).all()
    ready = [(entry_id, account) for entry_id, account in rows if mailbox_readiness_snapshot(account)["ready"]]
    if not ready:
        return []
    joined = set((await db.scalars(select(WorkspaceMembership.account_id).where(
        WorkspaceMembership.account_id.in_([account.id for _, account in ready]),
        WorkspaceMembership.membership_state == MEMBERSHIP_STATE_JOINED,
    ))).all())
    return [(entry_id, account.email) for entry_id, account in ready if account.id not in joined][:limit]


async def _refill(db: AsyncSession, client, config: dict[str, Any], state: dict[str, Any]) -> tuple[str | None, str | None]:
    """Launch at most one pool_join. Returns (operation public id, blocked reason)."""
    from app.application import pool_join
    from app.application.operations import operation_store

    if state["today"]["count"] >= config["daily_limit"]:
        return None, f"今日已达上限 {config['daily_limit']} 个"
    if await operation_store.browser_busy(db) is not None:
        return None, "浏览器正被占用"
    try:
        await db.commit()  # never hold a read transaction across network I/O
        proxies = await client.list_proxies()
    except Exception:  # noqa: BLE001
        await db.rollback()
        return None, "codex-rs 代理读取失败"
    if not any(isinstance(item.get("lastTest"), dict) and item["lastTest"].get("success") is True for item in proxies):
        return None, "codex-rs 没有测试通过的代理"
    if not config["workspace_ids"]:
        return None, "没有勾选补号目标团队"
    workspaces = await _candidate_workspaces(db, config["workspace_ids"])
    if not workspaces:
        return None, "目标团队都在忙或不可用"
    entries = await _candidate_entries(db, MAX_ENTRY_TRIES)
    if not entries:
        return None, "号池没有可拉入的号"
    await db.commit()

    reason = None
    for entry_id, email in entries:
        tried = 0
        for workspace_id in list(workspaces):
            if tried >= MAX_TEAM_TRIES:
                break
            tried += 1
            try:
                result = await pool_join.start_pool_join(
                    db, entry_id, workspace_id=workspace_id, role="member", seat_intent="workspace_default",
                    push_target="codex_rs", source=SOURCE,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("codex-rs auto refill: start_pool_join crashed entry_id=%s", entry_id)
                await db.rollback()
                return None, f"发起补号出错：{type(exc).__name__}"
            if result.get("operation_id") and "entry_id" in result:
                logger.info("codex-rs auto refill: pulling %s into workspace %s", email, workspace_id)
                return str(result["operation_id"]), None
            code = str(result.get("error_code") or "")
            reason = str(result.get("error") or "拉入没有启动")
            if code == "browser_busy":
                return None, "浏览器正被占用"
            if code in TEAM_FAILURE_CODES and not (code == "not_found" and "号池条目" in reason):
                workspaces.remove(workspace_id)
                continue
            break  # not_joinable or anything else: the account is the problem, try the next one
        if not workspaces:
            break
    return None, reason or "目标团队都在忙或不可用"
