"""号池数据：导入、邮箱检测、HME 打标、列表、推荐团队、移出号池。"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.application.mailbox import _find_hme_owner, mailbox_readiness_snapshot, probe_account_mailbox
from app.application.resources.hme import (
    HmeConfig,
    active_leased_emails,
    hme_client,
    load_config,
    resolve_workspace_tag,
)
from app.application.workspace_expiry import EXPIRY_TIMEZONE
from app.application.workspace_switch_count import switch_count_record, switch_gap_met
from app.core.time import isoformat, utcnow, zone
from app.domain.automation import ACTIVE_STATES, WORKSPACE_LOCK_ACTIONS
from app.domain.identity import MEMBERSHIP_STATE_JOINED, PROVIDER_SUB2API
from app.domain.identity.ids import normalize_email
from app.domain.resources import is_unoccupied_label
from app.domain.workspaces.names import resolve_display_name
from app.persistence.models.identity import (
    Account,
    ExternalBinding,
    Workspace,
    WorkspaceMembership,
    WorkspaceOfficialMemberSnapshot,
)
from app.persistence.models.operations import Operation
from app.persistence.models.pool import StandbyPoolEntry

logger = logging.getLogger(__name__)

POOL_LABEL = "GPT号池"
IMPORT_LIMIT = 50
_EMAIL_RE = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")

STATE_LABELS = {
    "pending": "待拉入",
    "joining": "拉入中",
    "joined": "已入组",
    "failed": "失败",
    "manual_required": "待人工",
}
SUMMARY_STATES = ("pending", "joining", "joined", "failed", "manual_required")


def _fail(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error_code": code, "error": message}


# ---------------------------------------------------------------- HME 标签


async def _tag_pool(cfg: HmeConfig, entry: StandbyPoolEntry, account: Account) -> None:
    """找到别名行，记下 anonymousId 和原标签；原标签未占用时打 GPT号池。不抛异常。"""
    try:
        owner_id, rows = await _find_hme_owner(cfg, normalize_email(account.email), str(account.hme_account_id or ""))
    except Exception:  # noqa: BLE001 - 打标失败不影响导入
        logger.exception("standby pool: HME alias lookup failed email=%s", account.email)
        entry.label_state = "failed"
        return
    row = rows[0] if rows else {}
    entry.hme_account_id = owner_id
    entry.anonymous_id = str(row.get("anonymousId") or row.get("anonymous_id") or "").strip() or None
    label = str(row.get("label") or "").strip()
    if label == POOL_LABEL:
        # 已是号池标签（例如上次打标其实成功了），不覆盖原标签记录。
        entry.label_state = "pool"
        return
    entry.original_label = label[:200] or None
    if not is_unoccupied_label(label):
        entry.label_state = "kept"
        return
    if not entry.anonymous_id:
        entry.label_state = "failed"
        return
    try:
        await asyncio.to_thread(hme_client.set_local_label, cfg, owner_id, entry.anonymous_id, POOL_LABEL)
    except Exception:  # noqa: BLE001
        logger.exception("standby pool: HME pool label failed email=%s", account.email)
        entry.label_state = "failed"
        return
    entry.label_state = "pool"


# ---------------------------------------------------------------- 列表上下文


@dataclass
class _Context:
    accounts: dict[int, Account] = field(default_factory=dict)
    ops: dict[str, Operation] = field(default_factory=dict)
    joined: set[int] = field(default_factory=set)
    related: set[int] = field(default_factory=set)
    remote_emails: set[str] = field(default_factory=set)
    bound: set[int] = field(default_factory=set)
    busy_ids: set[int] = field(default_factory=set)
    busy_emails: set[str] = field(default_factory=set)
    workspace_names: dict[int, str] = field(default_factory=dict)


async def _load_context(db: AsyncSession, entries: list[StandbyPoolEntry]) -> _Context:
    ctx = _Context()
    account_ids = [entry.account_id for entry in entries]
    emails = [normalize_email(entry.email) for entry in entries]
    if not entries:
        return ctx
    for account in (await db.scalars(select(Account).where(Account.id.in_(account_ids)))).all():
        ctx.accounts[account.id] = account
    public_ids = [entry.operation_public_id for entry in entries if entry.operation_public_id]
    if public_ids:
        for op in (await db.scalars(select(Operation).where(Operation.public_id.in_(public_ids)))).all():
            ctx.ops[op.public_id] = op
    rows = await db.execute(
        select(WorkspaceMembership.account_id, WorkspaceMembership.membership_state).where(
            WorkspaceMembership.account_id.in_(account_ids)
        )
    )
    for account_id, state in rows.all():
        ctx.related.add(int(account_id))
        if state == MEMBERSHIP_STATE_JOINED:
            ctx.joined.add(int(account_id))
    rows = await db.execute(
        select(func.lower(WorkspaceOfficialMemberSnapshot.normalized_email)).where(
            func.lower(WorkspaceOfficialMemberSnapshot.normalized_email).in_(emails),
            WorkspaceOfficialMemberSnapshot.remote_state.in_(("joined", "invited")),
        )
    )
    ctx.remote_emails = {normalize_email(row[0]) for row in rows.all() if row[0]}
    rows = await db.execute(
        select(ExternalBinding.local_account_id).where(
            ExternalBinding.provider == PROVIDER_SUB2API,
            ExternalBinding.local_account_id.in_(account_ids),
        )
    )
    ctx.bound = {int(row[0]) for row in rows.all()}
    rows = await db.execute(
        select(Operation.account_id, Operation.email).where(
            Operation.state.in_(ACTIVE_STATES),
            or_(Operation.account_id.in_(account_ids), func.lower(Operation.email).in_(emails)),
        )
    )
    for account_id, email in rows.all():
        if account_id is not None:
            ctx.busy_ids.add(int(account_id))
        if email:
            ctx.busy_emails.add(normalize_email(email))
    workspace_ids = {int(entry.workspace_id) for entry in entries if entry.workspace_id}
    if workspace_ids:
        workspaces = (await db.scalars(select(Workspace).where(Workspace.id.in_(workspace_ids)))).all()
        for workspace in workspaces:
            ctx.workspace_names[workspace.id] = await _display_name(db, workspace)
    return ctx


async def _display_name(db: AsyncSession, workspace: Workspace) -> str:
    owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
    return str(resolve_display_name(workspace, owner_email=owner.email if owner else None)["display_name"])


def _reconcile(entry: StandbyPoolEntry, ctx: _Context) -> bool:
    """拉入中但任务已结束时，按任务结果纠正条目状态。返回是否有改动。"""
    if entry.state != "joining" or not entry.operation_public_id:
        return False
    op = ctx.ops.get(entry.operation_public_id)
    if op is not None and op.state in ACTIVE_STATES:
        return False
    if op is None:
        entry.state = "failed"
        entry.error_code = "operation_missing"
        entry.error = "拉入任务记录不存在"
        return True
    if op.state == "success":
        entry.state = "joined"
        entry.error_code = None
        entry.error = None
        if entry.joined_at is None:
            entry.joined_at = op.finished_at or utcnow()
        return True
    entry.state = "manual_required" if op.state in {"manual_required", "partial"} else "failed"
    entry.error_code = (op.error_code or op.state or "")[:60] or None
    entry.error = str(op.error_message or "")[:500] or ("任务需要人工处理" if entry.state == "manual_required" else "拉入任务失败")
    return True


def _mailbox(account: Account) -> dict[str, Any]:
    snapshot = mailbox_readiness_snapshot(account)
    if snapshot["ready"]:
        state, error = "ready", None
    elif snapshot["read_state"] == "failed":
        state, error = "failed", "服务器读不到这个邮箱，请确认别名属于已接入的 HME 账号后重新检测"
    else:
        state, error = "unknown", "未检测"
    return {"state": state, "error": error, "checked_at": snapshot["checked_at"]}


def _serialize(entry: StandbyPoolEntry, account: Account, ctx: _Context) -> dict[str, Any]:
    mailbox = _mailbox(account)
    email = normalize_email(entry.email)
    workspace = None
    if entry.workspace_id:
        workspace = {
            "id": entry.workspace_id,
            "name": ctx.workspace_names.get(int(entry.workspace_id)) or f"团队 #{entry.workspace_id}",
        }
    joined = account.id in ctx.joined
    removable = (
        entry.state in {"pending", "failed"}
        and account.id not in ctx.related
        and email not in ctx.remote_emails
        and account.id not in ctx.bound
        and account.id not in ctx.busy_ids
        and email not in ctx.busy_emails
    )
    return {
        "id": entry.id,
        "account_id": entry.account_id,
        "email": entry.email,
        "mailbox": mailbox,
        "state": entry.state,
        "state_label": STATE_LABELS.get(entry.state, entry.state),
        "workspace": workspace,
        "operation_id": entry.operation_public_id,
        "error_code": entry.error_code,
        "error": entry.error,
        "label_state": entry.label_state,
        "imported_at": isoformat(entry.imported_at),
        "joined_at": isoformat(entry.joined_at),
        "can_join": entry.state in {"pending", "failed"} and mailbox["state"] == "ready" and not joined,
        "can_continue": entry.state in {"failed", "manual_required"} and bool(entry.workspace_id),
        "can_remove": removable,
    }


async def _build_items(db: AsyncSession, entries: list[StandbyPoolEntry]) -> list[dict[str, Any]]:
    ctx = await _load_context(db, entries)
    changed = False
    for entry in entries:
        changed = _reconcile(entry, ctx) or changed
    if changed:
        await db.commit()
    return [_serialize(entry, ctx.accounts[entry.account_id], ctx) for entry in entries if entry.account_id in ctx.accounts]


async def _entry_item(db: AsyncSession, entry: StandbyPoolEntry) -> dict[str, Any] | None:
    items = await _build_items(db, [entry])
    return items[0] if items else None


# ---------------------------------------------------------------- 接口


async def list_entries(db: AsyncSession) -> dict[str, Any]:
    """列出号池条目和各状态计数。"""
    entries = list(
        (
            await db.scalars(
                select(StandbyPoolEntry).order_by(StandbyPoolEntry.imported_at.desc(), StandbyPoolEntry.id.desc())
            )
        ).all()
    )
    items = await _build_items(db, entries)
    summary = {"total": len(items), **{state: 0 for state in SUMMARY_STATES}, "mailbox_failed": 0}
    for item in items:
        if item["state"] in summary:
            summary[item["state"]] += 1
        if item["mailbox"]["state"] == "failed":
            summary["mailbox_failed"] += 1
    return {"items": items, "summary": summary}


def _parse_lines(text: str) -> tuple[list[str], list[str]]:
    emails: list[str] = []
    invalid: list[str] = []
    seen: set[str] = set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        email = normalize_email(line)
        if len(email) > 255 or not _EMAIL_RE.match(email):
            invalid.append(line[:200])
            continue
        if email in seen:
            continue
        seen.add(email)
        emails.append(email)
    return emails, invalid


async def import_emails(db: AsyncSession, text: str) -> dict[str, Any]:
    """按行导入邮箱，建本地待命账号并检测收件。"""
    emails, invalid = _parse_lines(text)
    if len(emails) > IMPORT_LIMIT:
        return _fail("too_many", f"单次最多导入 {IMPORT_LIMIT} 个")
    imported: list[str] = []
    skipped: list[dict[str, str]] = []
    mailbox_failed: list[dict[str, str]] = []
    if not emails:
        return {"ok": True, "imported": imported, "skipped": skipped, "invalid": invalid, "mailbox_failed": mailbox_failed}

    existing = {
        normalize_email(row[0])
        for row in (await db.execute(select(Account.email).where(func.lower(Account.email).in_(emails)))).all()
        if row[0]
    }
    existing |= {
        normalize_email(row[0])
        for row in (
            await db.execute(select(StandbyPoolEntry.email).where(func.lower(StandbyPoolEntry.email).in_(emails)))
        ).all()
        if row[0]
    }
    leased = await active_leased_emails(db)
    cfg = await load_config(db)

    for email in emails:
        if email in existing:
            skipped.append({"email": email, "reason": "本地已有账号"})
            continue
        if email in leased:
            skipped.append({"email": email, "reason": "HME 正在被其他任务领用"})
            continue
        try:
            account = Account(
                email=email,
                local_purpose="standby",
                operational_state="available",
                auth_state="unknown",
                official_plan="unknown",
            )
            db.add(account)
            await db.flush()
            entry = StandbyPoolEntry(account_id=account.id, email=email, state="pending", label_state="none")
            db.add(entry)
            await db.flush()
            await db.commit()
        except Exception as exc:  # noqa: BLE001 - 单个邮箱失败不影响其他邮箱
            await db.rollback()
            logger.exception("standby pool: import failed email=%s", email)
            skipped.append({"email": email, "reason": f"保存失败：{str(exc)[:120]}"})
            continue
        imported.append(email)
        try:
            probe = await probe_account_mailbox(db, account.id)
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("standby pool: mailbox probe crashed email=%s", email)
            probe = {"ok": False, "error": "邮箱检测出错"}
        if not probe.get("ok"):
            mailbox_failed.append({"email": email, "error": str(probe.get("error") or "邮箱不可读")})
            continue
        try:
            await _tag_pool(cfg, entry, account)
            await db.commit()
        except Exception:  # noqa: BLE001
            await db.rollback()
            logger.exception("standby pool: label bookkeeping failed email=%s", email)
    return {"ok": True, "imported": imported, "skipped": skipped, "invalid": invalid, "mailbox_failed": mailbox_failed}


async def recheck_mailbox(db: AsyncSession, entry_id: int) -> dict[str, Any]:
    """重新检测某条目的邮箱能否被服务器读信。"""
    entry = await db.get(StandbyPoolEntry, int(entry_id))
    if entry is None:
        return _fail("not_found", "号池条目不存在")
    account = await db.get(Account, entry.account_id)
    if account is None:
        return _fail("not_found", "本地账号已删除")
    probe = await probe_account_mailbox(db, account.id)
    retag = not entry.anonymous_id or (entry.label_state in {"none", "failed"} and entry.state in {"pending", "failed"})
    if probe.get("ok") and retag:
        cfg = await load_config(db)
        await _tag_pool(cfg, entry, account)
        await db.commit()
    item = await _entry_item(db, entry)
    if item is None:
        return _fail("not_found", "本地账号已删除")
    if not probe.get("ok"):
        item["mailbox"]["error"] = str(probe.get("error") or item["mailbox"]["error"] or "邮箱不可读")
    return {"ok": True, "item": item}


SEAT_BY_OFFICIAL = {"default": "standard", "prolite": "premium"}


async def _replace_candidates(db: AsyncSession, workspace: Workspace, owner: Account | None) -> list[dict[str, Any]]:
    """上次同步快照里的已入组成员（不含母号），供"替换子号"选择并继承角色 / 席位。"""
    owner_email = normalize_email(owner.email) if owner else ""
    rows = (
        await db.scalars(
            select(WorkspaceOfficialMemberSnapshot)
            .where(
                WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id,
                WorkspaceOfficialMemberSnapshot.remote_state == "joined",
            )
            .order_by(WorkspaceOfficialMemberSnapshot.normalized_email)
        )
    ).all()
    members = []
    for row in rows:
        email = normalize_email(row.normalized_email)
        if not email or email == owner_email:
            continue
        role = str(row.official_role or "").lower()
        members.append({
            "email": email,
            "role": role if role in {"owner", "member"} else "member",
            "seat_intent": SEAT_BY_OFFICIAL.get(str(row.seat_type or ""), "workspace_default"),
        })
    return members


def _expired(workspace: Workspace) -> bool:
    if not workspace.manual_expires_on:
        return False
    today = utcnow().astimezone(zone(EXPIRY_TIMEZONE)).date()
    return workspace.manual_expires_on < today


async def recommend_workspaces(db: AsyncSession, entry_id: int) -> dict[str, Any]:
    """给拉入弹框列出候选团队并给出推荐团队。"""
    from app.application.manual_rotation import unresolved_for_workspace
    from app.application.operations import operation_store

    entry = await db.get(StandbyPoolEntry, int(entry_id))
    if entry is None:
        return _fail("not_found", "号池条目不存在")
    workspaces = (
        await db.scalars(select(Workspace).where(Workspace.status == "active").order_by(Workspace.id))
    ).all()
    eligible: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    for workspace in workspaces:
        owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
        limit = workspace.seat_limit
        occupied = workspace.occupied_seats
        reason = ""
        if owner is None or not owner.proxy:
            reason = "母号未配置代理"
        elif not workspace.official_workspace_id:
            reason = "团队未同步"
        elif await operation_store.active_for_workspace(db, workspace.id, actions=WORKSPACE_LOCK_ACTIONS) is not None:
            reason = "有进行中任务"
        elif await unresolved_for_workspace(db, workspace.id) is not None:
            reason = "有未完成轮转"
        elif _expired(workspace):
            reason = "已到期"
        name = resolve_display_name(workspace, owner_email=owner.email if owner else None)["display_name"]
        switch = switch_count_record(workspace)
        item = {
            "id": workspace.id,
            "name": str(name),
            "occupied": occupied,
            "limit": limit,
            "switch_count": int(switch["count"]),
            "last_switched_at": switch["last_switched_at"],
            "switch_gap_met": switch_gap_met(workspace),
            # 满员（或席位未知）不挡：拉入时选一个子号替换，先移出再邀请。
            "full": limit is not None and occupied is not None and occupied >= limit,
            "members": await _replace_candidates(db, workspace, owner) if not reason else [],
            "eligible": not reason,
            "reason": reason or None,
        }
        (blocked if reason else eligible).append(item)
    # 距上次切换已满 8h（或无记录）的优先，再按今日切换少、空位多。只影响推荐，不拦选择。
    eligible.sort(key=lambda item: (not item["switch_gap_met"], item["switch_count"],
                                    -((item["limit"] or 0) - (item["occupied"] or 0)), item["id"]))
    return {
        "recommended_id": eligible[0]["id"] if eligible else None,
        "workspaces": eligible + blocked,
    }


async def remove_entry(db: AsyncSession, entry_id: int) -> dict[str, Any]:
    """移出从未入组过的号池条目，同时删本地档案。"""
    from app.application.account_deletion import AccountDeletionError, delete_unassigned_account

    entry = await db.get(StandbyPoolEntry, int(entry_id))
    if entry is None:
        return _fail("not_found", "号池条目不存在")
    account = await db.get(Account, entry.account_id)
    if account is not None:
        item = await _entry_item(db, entry)
        if item is None or not item["can_remove"]:
            return _fail("not_joinable", "已拉入过团队或有任务记录，不能移出")
    hme_account_id = entry.hme_account_id or (account.hme_account_id if account else None)
    anonymous_id = entry.anonymous_id
    original_label = str(entry.original_label or "").strip()
    email = entry.email
    restore = entry.label_state == "pool" and bool(anonymous_id and hme_account_id)
    cfg = await load_config(db) if restore else None
    warning = None
    restored = False
    # 先恢复 HME 标签（不占数据库写锁），再删条目和本地档案；删除被守卫拦下时把 GPT号池 标签补回去。
    if restore:
        try:
            await asyncio.to_thread(hme_client.set_local_label, cfg, hme_account_id, anonymous_id, original_label)
            restored = True
        except Exception:  # noqa: BLE001 - 标签没恢复不挡移出
            logger.exception("standby pool: restore label failed email=%s", email)
            if original_label:
                warning = f"HME 标签没能恢复成「{original_label}」，别名仍是「{POOL_LABEL}」，请到 HME 资源页手动改回"
            else:
                warning = f"HME 不接受清空标签，别名仍保留「{POOL_LABEL}」标签，不会被自动领号"
    try:
        await db.delete(entry)
        await db.flush()
        if account is not None:
            await delete_unassigned_account(db, account.id)
    except AccountDeletionError as exc:
        await db.rollback()
        if restored:
            try:
                await asyncio.to_thread(hme_client.set_local_label, cfg, hme_account_id, anonymous_id, POOL_LABEL)
            except Exception:  # noqa: BLE001
                logger.exception("standby pool: re-apply pool label failed email=%s", email)
        return _fail(exc.code, str(exc))
    await db.commit()
    result: dict[str, Any] = {"ok": True, "email": email}
    if warning:
        result["warning"] = warning
    return result


async def apply_team_label(db: AsyncSession, entry_id: int, workspace_id: int) -> None:
    """把别名标签改成团队标签；失败只记 label_state="failed"，不抛异常。"""
    entry = None
    try:
        entry = await db.get(StandbyPoolEntry, int(entry_id))
        if entry is None or entry.label_state not in {"pool", "failed"} or not entry.anonymous_id:
            return None
        hme_account_id = entry.hme_account_id
        if not hme_account_id:
            account = await db.get(Account, entry.account_id)
            hme_account_id = account.hme_account_id if account else None
        if not hme_account_id:
            entry.label_state = "failed"
            return None
        workspace = await db.scalar(
            select(Workspace).options(selectinload(Workspace.owner_account)).where(Workspace.id == int(workspace_id))
        )
        if workspace is None:
            entry.label_state = "failed"
            return None
        cfg = await load_config(db)
        tag = resolve_workspace_tag(workspace, cfg.team_tag_map)
        await asyncio.to_thread(hme_client.set_local_label, cfg, hme_account_id, entry.anonymous_id, tag)
        entry.label_state = "team"
    except Exception:  # noqa: BLE001
        logger.exception("standby pool: team label failed entry=%s workspace=%s", entry_id, workspace_id)
        if entry is not None:
            entry.label_state = "failed"
    return None
