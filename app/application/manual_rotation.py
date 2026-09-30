"""Manual one-for-one rotation: the operator picks the old member and types one new mailbox.

Stages run in a fixed order and each confirmed stage is recorded on the root
Operation, so "continue" only runs what is still unconfirmed:

    preflight -> paused -> kicked -> vacancy -> joined/authorized
              -> published -> old_remote_deleted -> counted

The old Sub2API account is paused before the kick but deleted only after the
new account reads back healthy. The replacement mailbox is used as typed: no
standby pool, no HME claim or label, no phone pool; codes come from Cloudflare.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.application import sub2api_status
from app.application.automatic_rotation import publish_replacement, refresh_after_rotation
from app.application.connection_probe import probe_mail
from app.application.extension_runner import check_runner_proxy, runner_enabled, validate_runner_configuration
from app.application.identity import automation_gate
from app.application.member_handoff import token_workspace_mismatch
from app.application.member_lifecycle import has_other_active_context
from app.application.operations import operation_store, unpack_input
from app.application.reauth import load_cf_config
from app.application.revenue_ledger import revenue_ledger
from app.application.sub2api_credential_sync import validate_sync_identity
from app.application.sub2api_defaults import load_defaults, validate_defaults
from app.application.tokens import decrypt_secret
from app.application.workspace_switch_count import increment_workspace_switch_count
from app.core.config import load_settings
from app.core.jwt import jwt_parser
from app.core.proxy import inherit_proxy_url
from app.core.time import as_utc, isoformat, utcnow
from app.domain.automation import ACTIVE_STATES, BROWSER_ACTIONS, WORKSPACE_LOCK_ACTIONS
from app.domain.identity import MEMBERSHIP_STATE_JOINED
from app.domain.identity.ids import normalize_email
from app.domain.identity.policy import is_workspace_owner
from app.domain.onboard import KICK_COOLDOWN_SECONDS
from app.domain.rotate import manual_rotation_email_error
from app.domain.vacancy import is_billable_vacancy, is_safe_to_refill
from app.integrations.mail.otp import parse_mail_line
from app.integrations.openai.browser.environment import BrowserEnvironmentError, validate_configuration
from app.integrations.openai.member_adapter import normalize_official_role
from app.integrations.openai.browser.signup import validate_signup_assets
from app.application.resources.proxies import proxy_profile_service
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from app.persistence.models.operations import Operation, OperationStep

logger = logging.getLogger(__name__)

FLOW_VERSION = "manual_rotation_v1"
SOURCE = "manual_rotation"
SIGNUP_FLOW = "extension"
SEAT_BY_OFFICIAL = {"default": "standard", "prolite": "premium"}
# A finished stage from this list means the team or Sub2API was already touched.
SIDE_EFFECT_STEPS = ("paused", "official_removed", "kicked", "joined", "authorized", "published", "old_remote_deleted")
UNRESOLVED_STATES = ("partial", "manual_required", "failed", "cancelled")
# Registration or OAuth stopped on something a person has to look at; keep the same mailbox.
MANUAL_MARKERS = ("phone", "sms", "challenge", "captcha", "turnstile", "mismatch", "conflict")


class RotationBlocked(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _default_rotate():
    from app.application.rotate import rotate_service

    return rotate_service


# ---------------------------------------------------------------- opening


async def _has_side_effects(db, op: Operation) -> bool:
    found = await db.scalar(select(OperationStep.id).where(
        OperationStep.operation_id == op.id,
        OperationStep.step_name.in_(SIDE_EFFECT_STEPS),
        OperationStep.state.in_(("success", "running", "failed")),
    ).limit(1))
    return found is not None


async def unresolved_for_workspace(db, workspace_id: int, *, exclude_public_id: str = "") -> Operation | None:
    """A manual rotation that touched the team and was not finished blocks the next one."""
    rows = await db.scalars(select(Operation).where(
        Operation.op_type == "rotate", Operation.source == SOURCE,
        Operation.workspace_id == int(workspace_id),
        Operation.state.in_(UNRESOLVED_STATES),
    ).order_by(Operation.id.desc()))
    for row in list(rows):
        if row.public_id == exclude_public_id:
            continue
        if row.state in {"partial", "manual_required"} or await _has_side_effects(db, row):
            return row
    return None


async def _replacement_in_use(db, email: str) -> Operation | None:
    """The same new mailbox cannot be assigned by two open rotations or a running onboard."""
    active = await operation_store.active_for_email(db, email)
    if active is not None:
        return active
    rows = await db.scalars(select(Operation).where(
        Operation.op_type == "rotate", Operation.source == SOURCE,
        Operation.state.in_(ACTIVE_STATES + UNRESOLVED_STATES),
    ))
    for row in list(rows):
        if normalize_email(unpack_input(row.input_json).get("replacement_email")) != email:
            continue
        if row.state in ACTIVE_STATES + ("partial", "manual_required") or await _has_side_effects(db, row):
            return row
    return None


async def _local_blocker(db, workspace: Workspace, old: str, new: str) -> dict[str, Any] | None:
    def blocked(code, message):
        return {"ok": False, "error_code": code, "error": message}

    owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
    if workspace.status != "active":
        return blocked("workspace_unavailable", "团队已停用")
    if owner is not None and normalize_email(owner.email) in {old, new}:
        return blocked("primary_mother_protected", "母号不能被轮转，也不能作为新邮箱")
    old_account = await db.scalar(select(Account).where(Account.email == old))
    if old_account is not None and is_workspace_owner(workspace, old_account.id):
        return blocked("primary_mother_protected", "不能轮转当前团队的主控母号")
    account = await db.scalar(select(Account).where(Account.email == new))
    if account is None:
        return None
    owned = await db.scalar(select(Workspace.id).where(Workspace.owner_account_id == account.id).limit(1))
    if account.local_purpose == "mother" or owned is not None:
        return blocked("replacement_is_mother", "新邮箱是母号，不能作为补位账号")
    if account.operational_state in {"disabled", "archived"}:
        return blocked("replacement_unavailable", "新邮箱的本地档案已停用或归档")
    member = await db.scalar(select(WorkspaceMembership.id).where(
        WorkspaceMembership.account_id == account.id,
        WorkspaceMembership.membership_state.in_(("joined", "invited")),
    ).limit(1))
    if member is not None:
        return blocked("replacement_in_team", "新邮箱已在某个团队中（已加入或已邀请），请先核对")
    if parse_mail_line(account.mail_raw or "").get("pickup_url"):
        return blocked("replacement_pickup_url", "新邮箱的本地档案带取件地址；本流程只从 Cloudflare 读取验证码")
    conflict = await db.scalar(select(ExternalBinding.id).where(
        ExternalBinding.local_account_id == account.id, ExternalBinding.binding_state == "conflict",
    ).limit(1))
    if conflict is not None:
        return blocked("identity_conflict", "新邮箱的 Sub2API 绑定存在身份冲突")
    if account.operational_state == "standby":
        now = utcnow()
        eligible = account.next_eligible_at
        recent = bool(account.updated_at and (now - as_utc(account.updated_at)).total_seconds() < KICK_COOLDOWN_SECONDS)
        if (eligible and as_utc(eligible) > now) or (eligible is None and recent):
            return blocked("kick_cooldown", "新邮箱刚被移出过团队，冷却期内不要再拉入")
    return None


async def open_rotation(db, workspace_id: int, *, email: str, replacement_email: str) -> tuple[Operation | None, dict[str, Any] | None]:
    """Validate input and local facts, then take the team lock. No network, no side effects."""
    # SQLite's write lock serializes the email check and task creation across teams.
    # Commit immediately after creation; no network call holds this transaction.
    await db.execute(update(Workspace).where(Workspace.id == int(workspace_id)).values(id=Workspace.id))
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None:
        return None, {"ok": False, "error": "workspace not found", "error_code": "not_found"}
    old, new = normalize_email(email), normalize_email(replacement_email)
    message = manual_rotation_email_error(old, new)
    if message:
        return None, {"ok": False, "error_code": "invalid_rotation_email", "error": message}
    blocked = await _local_blocker(db, workspace, old, new)
    if blocked:
        return None, blocked
    pending = await unresolved_for_workspace(db, workspace.id)
    if pending is not None:
        return None, {"ok": False, "error_code": "rotation_unresolved", "operation_id": pending.public_id,
                      "error": f"本团队还有未完成的轮转 {pending.public_id}，请先继续原轮转；归档不会解除占用"}
    in_use = await _replacement_in_use(db, new)
    if in_use is not None:
        return None, {"ok": False, "error_code": "replacement_in_use", "operation_id": in_use.public_id,
                      "error": f"新邮箱正被任务 {in_use.public_id} 使用"}
    old_account = await db.scalar(select(Account).where(Account.email == old))
    operation, blocker = await operation_store.create_workspace_locked(
        db, op_type="rotate", workspace_id=workspace.id,
        account_id=old_account.id if old_account else None, email=old, source=SOURCE,
        input_payload={
            "flow_version": FLOW_VERSION, "workspace_id": workspace.id, "old_email": old,
            "old_account_id": old_account.id if old_account else None,
            "replacement_email": new, "signup_flow": SIGNUP_FLOW,
        },
    )
    if blocker is not None:
        return None, {"ok": False, "error_code": "operation_conflict", "operation_id": blocker.public_id,
                      "error": f"团队已有 {blocker.op_type} 任务 {blocker.public_id} 在跑"}
    await db.commit()
    return operation, None


def can_continue(row: Operation) -> bool:
    return bool(row.op_type == "rotate" and row.source == SOURCE
                and row.state in UNRESOLVED_STATES)


async def unresolved_for_replacement(db, email: str) -> Operation | None:
    """The unfinished rotation waiting on this new mailbox, if any."""
    target = normalize_email(email)
    rows = await db.scalars(select(Operation).where(
        Operation.op_type == "rotate", Operation.source == SOURCE,
        Operation.state.in_(UNRESOLVED_STATES),
    ).order_by(Operation.id.desc()))
    for row in list(rows):
        if normalize_email(unpack_input(row.input_json).get("replacement_email")) != target:
            continue
        if row.state in {"partial", "manual_required"} or await _has_side_effects(db, row):
            return row
    return None


async def reopen_rotation(db, public_id: str) -> tuple[Operation | None, dict[str, Any] | None]:
    """Put an unfinished manual rotation back under the team lock without replaying it."""
    # Serialize continuation claims before reading state.
    await db.execute(update(Operation).where(Operation.public_id == public_id).values(id=Operation.id))
    op = await operation_store.get_by_public_id(db, public_id)
    if op is not None:
        await db.refresh(op)
    if op is None:
        return None, {"ok": False, "error": "operation not found", "error_code": "not_found"}
    if not can_continue(op) or unpack_input(op.input_json).get("flow_version") != FLOW_VERSION:
        return None, {"ok": False, "error_code": "continue_unsupported",
                      "error": "只有未完成的新版手动轮转任务可以继续"}
    if op.state in {"failed", "cancelled"} and not await _has_side_effects(db, op):
        return None, {"ok": False, "error_code": "continue_unsupported", "error": "该任务未改动团队，请重新发起轮转"}
    busy = await operation_store.active_for_workspace(db, op.workspace_id, actions=WORKSPACE_LOCK_ACTIONS)
    if busy is not None:
        return None, {"ok": False, "error_code": "operation_conflict", "operation_id": busy.public_id,
                      "error": f"团队已有 {busy.op_type} 任务 {busy.public_id} 在跑"}
    try:
        async with db.begin_nested():
            op.state = "running"
            op.archived_at = None
            op.archive_reason = None
            op.cancel_requested = False
            op.finished_at = None
            op.error_code = None
            op.error_message = None
            op.idempotency_key = f"ws-mutation:{op.workspace_id}"
            await operation_store.note(db, op, "continue", "继续未完成的轮转，只执行尚未确认的阶段")
            await db.flush()
    except IntegrityError:
        await db.refresh(op)
        return None, {"ok": False, "error_code": "operation_conflict", "error": "团队已有其他变更任务"}
    await db.commit()
    return op, None


# ---------------------------------------------------------------- preflight


BINDING_REASONS = {
    "binding_not_verified": "本地绑定未验证",
    "duplicate_workspace_bindings": "本团队下有多条绑定",
    "unscoped_or_foreign_bindings": "绑定没有记录所属团队",
    "remote_identity_unknown": "读取远端账号失败",
    "workspace_missing": "团队不存在",
}


async def _drop_binding(db, row: ExternalBinding) -> None:
    from sqlalchemy import delete
    from app.persistence.models.sub2api import Sub2ApiSyncObservation, Sub2ApiUsageSnapshot
    from app.persistence.models.sub2api_status import Sub2ApiAccountStatus

    # The remote is already gone; book what the local usage cache last saw before dropping it.
    if row.binding_state == "verified" and row.workspace_id is not None:
        await revenue_ledger.settle_binding(db, row, source="binding_cleanup", allow_remote=False)
    # SQLite does not enforce the CASCADE here; clear the per-binding caches explicitly.
    for model in (Sub2ApiSyncObservation, Sub2ApiUsageSnapshot, Sub2ApiAccountStatus):
        await db.execute(delete(model).where(model.binding_id == row.id))
    await db.delete(row)


async def _heal_old_binding(db, rotate, account: Account, workspace: Workspace) -> None:
    """Rebuild the old account's local binding from a complete Sub2API catalog.

    Only local bookkeeping changes. Stale rows whose remote id is gone are dropped;
    a single remote whose email, platform and team all match is bound as verified.
    Anything less certain is left as-is so the checks after this still block.
    """
    from app.domain.identity.binding import (
        cross_check_binding, remote_email_from, remote_id_from, remote_official_account_id_from,
    )

    try:
        remotes = await rotate.sub2api.list_status_accounts(db)
    except Exception:  # noqa: BLE001
        return
    if not isinstance(remotes, list) or any(
            not isinstance(item, dict) or not remote_id_from(item).isdigit() for item in remotes):
        return
    remote_ids = {remote_id_from(item) for item in remotes}
    email = normalize_email(account.email)
    candidates = [item for item in remotes if remote_email_from(item) == email]
    if len(candidates) > 1:
        return
    rows = list(await db.scalars(select(ExternalBinding).where(
        ExternalBinding.provider == "sub2api", ExternalBinding.local_account_id == account.id,
        (ExternalBinding.workspace_id == workspace.id) | ExternalBinding.workspace_id.is_(None),
    )))
    target_id = remote_id_from(candidates[0]) if candidates else ""
    remote = None
    if target_id:
        try:
            remote = await rotate.sub2api.get_account(db, int(target_id))
        except Exception:  # noqa: BLE001
            return
        if validate_sync_identity(remote, target_id, email, workspace.official_workspace_id):
            return
        state, _ = cross_check_binding(
            local_email=email, local_official_account_id=account.official_account_id,
            expected_workspace=str(workspace.official_workspace_id or "").lower() or None, remote=remote,
        )
        if state != "verified":
            return
        owner = await db.scalar(select(ExternalBinding).where(
            ExternalBinding.provider == "sub2api", ExternalBinding.remote_account_id == target_id))
        if owner is not None and owner.local_account_id != account.id:
            return
        if owner is not None and owner not in rows:
            return  # Bound under another team of the same account; leave it to a person.
    kept = []
    for row in rows:
        if str(row.remote_account_id) != target_id and str(row.remote_account_id) not in remote_ids:
            await _drop_binding(db, row)
        else:
            kept.append(row)
    if target_id:
        if any(str(r.remote_account_id) != target_id for r in kept):
            await db.commit()  # Keep the stale-row cleanup; a live row for another remote needs a person.
            return
        await db.flush()
        row = kept[0] if kept else ExternalBinding(provider="sub2api", local_account_id=account.id,
                                                   remote_account_id=target_id)
        if not kept:
            db.add(row)
        row.workspace_id = workspace.id
        row.binding_state = "verified"
        row.verified_email = email
        row.verified_official_account_id = remote_official_account_id_from(remote) or None
        row.verified_workspace_id = str(workspace.official_workspace_id or "").lower() or None
        row.last_error = None
        row.last_observed_at = utcnow()
    if target_id or len(kept) != len(rows):
        await db.commit()


async def preflight(db, rotate, workspace_id: int, old: str, new: str) -> dict[str, Any]:
    """Live checks before anything is paused or kicked. Raises RotationBlocked."""
    workspace = await rotate.workspaces.load_workspace(db, workspace_id)
    if workspace is None or workspace.status != "active":
        raise RotationBlocked("workspace_unavailable", "团队不存在或已停用")
    owner = await rotate.workspaces.owner_account(db, workspace)
    if owner is None or not owner.proxy or not owner.access_token_encrypted:
        raise RotationBlocked("owner_not_ready", "母号凭据或代理未就绪")
    if normalize_email(owner.email) in {old, new}:
        raise RotationBlocked("primary_mother_protected", "母号不能被轮转，也不能作为新邮箱")
    signup_runner = "extension" if runner_enabled() else "playwright"
    try:
        if signup_runner == "extension":
            # Chromix runner: reject unusable config or an authenticated HTTP proxy before any kick.
            # The frozen proxy prefers the replacement's own proxy, then the owner's (proxy_profile_service.freeze).
            validate_runner_configuration()
            replacement = await db.scalar(select(Account).where(Account.email == new))
            await check_runner_proxy(inherit_proxy_url(replacement.proxy if replacement else "", owner.proxy))
        else:
            validate_configuration(load_settings())
            validate_signup_assets()
    except BrowserEnvironmentError as exc:
        raise RotationBlocked(exc.error_code, str(exc)) from None
    if not all((await load_cf_config(db)).values()):
        raise RotationBlocked("mail_missing", "Cloudflare 验证码邮箱未配置")
    mail = await probe_mail(db)
    if not mail.get("ok"):
        raise RotationBlocked("mail_unreachable", f"Cloudflare 邮箱读取失败：{mail.get('error') or '未知原因'}")
    sub_config = await rotate.sub2api.load_config(db)
    if not sub_config.get("configured"):
        raise RotationBlocked("sub2api_missing", "Sub2API 未配置")
    try:
        await validate_defaults(db, await load_defaults(db))
    except ValueError as exc:
        raise RotationBlocked("sub2api_defaults_invalid", str(exc)) from None

    live, member = await rotate.workspaces.lookup_live_member(db, workspace, old)
    if not live.get("success"):
        raise RotationBlocked("member_lookup_unknown", "官方成员读取失败")
    if not member or member.get("status") != "joined":
        raise RotationBlocked("old_not_joined", "旧号不是该团队的已加入成员")
    guard = rotate.workspaces._last_owner_guard(workspace, owner, live, member, email=old)
    if guard is not None:
        raise RotationBlocked(guard.get("error_code") or "kick_blocked", guard.get("error") or "不能移出该成员")
    role = normalize_official_role(member.get("role"))
    if role not in {"owner", "member"}:
        raise RotationBlocked("role_unsupported", f"旧号官方角色 {role} 不在可继承范围")
    seat = SEAT_BY_OFFICIAL.get(member.get("seat_type"))
    if seat is None:
        raise RotationBlocked("seat_unknown", "旧号席位类型未确认，请先同步团队")

    live_new, present = await rotate.workspaces.lookup_live_member(db, workspace, new)
    if not live_new.get("success"):
        raise RotationBlocked("member_lookup_unknown", "无法确认新邮箱的官方成员和邀请状态")
    if present:
        raise RotationBlocked("replacement_already_present", "新邮箱在官方团队里已有成员或邀请记录，请先核对")

    old_account = await db.scalar(select(Account).where(Account.email == old))
    old_binding_id, old_remote_id, stale_remote_id = None, "", ""
    if old_account is not None:
        if await has_other_active_context(db, old_account, workspace.id):
            raise RotationBlocked("old_other_context", "旧号还关联其他团队，本流程不处理，请人工核对")
        await _heal_old_binding(db, rotate, old_account, workspace)
        gate = await automation_gate(db, email=old, workspace_id=workspace.id)
        if not gate.get("allow"):
            raise RotationBlocked(gate.get("error_code") or "identity_conflict", gate.get("reason") or "旧号身份核对未通过")
        binding = await rotate._remote_binding_for(db, old_account, workspace_id=workspace.id)
        row = binding.get("binding")
        if binding.get("state") == "matched":
            old_binding_id = binding["binding"].id
            old_remote_id = str(binding.get("remote_id") or "")
        elif row is not None and row.binding_state == "missing" and row.workspace_id == workspace.id:
            # Reconcile already found the bound remote deleted; re-prove absence below
            # against the full catalog (both email and remote id) before treating it as "no remote".
            stale_remote_id = str(row.remote_account_id or "").strip()
        elif binding.get("state") != "absent":
            reason = binding.get("reason") or "unknown"
            raise RotationBlocked("old_binding_unverified",
                                  f"旧号的 Sub2API 绑定不明确（{BINDING_REASONS.get(reason, reason)}）")
    if not old_remote_id:
        # Missing local binding is not proof that the remote account does not exist.
        try:
            remotes = await rotate.sub2api.list_status_accounts(db)
        except Exception:
            raise RotationBlocked("old_remote_unknown", "无法读取 Sub2API 账号目录，不能确认旧号不存在") from None
        from app.domain.identity.binding import remote_email_from, remote_id_from
        if stale_remote_id and (not isinstance(remotes, list) or any(
                not isinstance(item, dict) or not remote_id_from(item).isdigit() for item in remotes)):
            raise RotationBlocked("old_remote_unknown", "Sub2API 账号目录不完整，不能确认旧号远端已删除")
        if any(normalize_email(remote_email_from(item)) == old for item in remotes):
            raise RotationBlocked("old_binding_unverified",
                                  "Sub2API 里有旧邮箱的账号，但无法自动确认是哪一个（可能有多个或团队不一致），请到 Sub2API 核对")
        if stale_remote_id and any(remote_id_from(item) == stale_remote_id for item in remotes):
            raise RotationBlocked("old_binding_unverified", f"旧号原绑定的远端账号 #{stale_remote_id} 仍在 Sub2API，请先重新对账")
    return {
        "workspace_id": workspace.id,
        "official_workspace_id": workspace.official_workspace_id,
        "old_email": old,
        "old_account_id": old_account.id if old_account else None,
        "old_user_id": member.get("user_id"),
        "role": role,
        "seat_intent": seat,
        "official_seat_type": member.get("seat_type"),
        "old_binding_id": old_binding_id,
        "old_remote_id": old_remote_id,
        "sub2api_source": sub2api_status.source_signature(sub_config),
        "replacement_email": new,
        "signup_flow": SIGNUP_FLOW,
        "signup_runner": signup_runner,
        "observed_at": isoformat(utcnow()),
    }


# ---------------------------------------------------------------- running


class _Rotation:
    def __init__(self, db, op: Operation, rotate, *, confirm_vacancy: bool, in_test: bool):
        self.db, self.op, self.rotate = db, op, rotate
        self.public_id = op.public_id
        self.confirm_vacancy = confirm_vacancy
        self.in_test = in_test
        self.input = unpack_input(op.input_json)
        self.ctx: dict[str, Any] = {}
        self.session = None

    # -- step records

    async def fresh(self) -> None:
        # Nested services may roll the session back; reload before touching the row.
        await self.db.refresh(self.op)

    async def step(self, name: str) -> OperationStep | None:
        return await self.db.scalar(select(OperationStep).where(
            OperationStep.operation_id == self.op.id, OperationStep.step_name == name))

    async def done(self, name: str) -> bool:
        row = await self.step(name)
        return row is not None and row.state == "success"

    async def step_result(self, name: str) -> dict[str, Any]:
        row = await self.step(name)
        try:
            data = json.loads(row.result_snapshot or "{}") if row is not None else {}
        except (TypeError, ValueError):
            data = {}
        return data if isinstance(data, dict) else {}

    async def mark(self, name: str, state: str, result: dict[str, Any] | None = None, *,
                   code: str = "", error: str = "", commit: bool = True) -> None:
        await self.fresh()
        await operation_store.mark_step(self.db, self.op, name, state=state, result=result,
                                        error_code=code, error_message=error)
        if commit:
            await self.db.commit()

    async def note(self, stage: str, message: str) -> None:
        await self.fresh()
        await operation_store.note(self.db, self.op, stage, message)
        await self.db.commit()

    def result(self, *, success: bool, status: str, code: str = "", error: str = "", **extra) -> dict[str, Any]:
        old = self.ctx.get("old_email") or self.input.get("old_email")
        new = self.ctx.get("replacement_email") or self.input.get("replacement_email")
        payload = {
            "success": success, "status": status, "partial": status == "partial",
            "rotation": {"flow_version": FLOW_VERSION, "old_email": old, "replacement_email": new,
                         "role": self.ctx.get("role"), "seat_intent": self.ctx.get("seat_intent")},
            "kick": {"child": {"email": old}},
            "invite": {"child": {"email": new}},
            **extra,
        }
        if not success:
            payload.update(error_code=code, error=error)
        return payload

    def stop(self, status: str, code: str, error: str, **extra) -> dict[str, Any]:
        return self.result(success=False, status=status, code=code, error=error, **extra)

    async def touched(self, code: str, error: str, *, manual: bool = False, **extra) -> dict[str, Any]:
        """Stop after the team was changed: always resumable, never reported as success."""
        return self.stop("manual_required" if manual else "partial", code, error, rotated=await self.done("kicked"), **extra)

    async def cancel_point(self) -> dict[str, Any] | None:
        await self.fresh()
        if not self.op.cancel_requested:
            return None
        if await _has_side_effects(self.db, self.op):
            return await self.touched("cancel_after_side_effect", "已在安全点停止；已完成的步骤不会回滚，可核对后继续")
        return self.stop("cancelled", "cancelled", "已取消，未改动团队或 Sub2API")

    async def replacement(self) -> Account | None:
        return await self.db.scalar(select(Account).where(Account.email == self.ctx["replacement_email"]))

    async def workspace(self) -> Workspace | None:
        return await self.db.get(Workspace, int(self.op.workspace_id))

    # -- orchestration

    async def run(self) -> dict[str, Any]:
        if self.input.get("flow_version") != FLOW_VERSION:
            return self.stop("failed", "continue_unsupported", "任务不是新版手动轮转")
        try:
            return await self._run()
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if task is not None and task.cancelling():
                raise  # Shutdown: the runner records it for manual review.
            # A cancel request surfaced from inside the browser flow.
            await self.db.rollback()
            await self.fresh()
            if await _has_side_effects(self.db, self.op):
                return await self.touched("cancel_after_side_effect", "已在安全点停止；已完成的步骤不会回滚，可核对后继续")
            return self.stop("cancelled", "cancelled", "已取消，未改动团队或 Sub2API")
        except Exception:  # noqa: BLE001
            logger.exception("manual rotation failed operation_id=%s", self.public_id)
            await self.db.rollback()
            await self.fresh()
            if await _has_side_effects(self.db, self.op):
                return await self.touched("rotation_failed", "轮转中断，已完成的步骤已保留；请核对后继续，不会重复踢人或注册", manual=True)
            return self.stop("failed", "rotation_failed", "轮转未开始改动团队就中断了，请核对后重新发起")
        finally:
            await self.release_browser()

    async def _run(self) -> dict[str, Any]:
        if await self.done("preflight"):
            self.ctx = await self.step_result("preflight")
        else:
            await self.note("preflight", "正在核对团队、旧号、新邮箱和依赖资源")
            try:
                self.ctx = await preflight(self.db, self.rotate, self.op.workspace_id,
                                           self.input.get("old_email"), self.input.get("replacement_email"))
            except RotationBlocked as exc:
                await self.db.rollback()
                await self.mark("preflight", "failed", {"error_code": exc.code}, code=exc.code, error=str(exc))
                return self.stop("failed", exc.code, f"{exc}（预检未通过，未暂停、未移出任何账号）")
            replacement = await self.db.scalar(select(Account).where(Account.email == self.ctx["replacement_email"]))
            workspace = await self.workspace()
            owner = await self.db.get(Account, workspace.owner_account_id)
            _, profile_id = await proxy_profile_service.freeze(
                self.db, job_id=self.public_id, child_proxy=replacement.proxy if replacement else "",
                mother_proxy=owner.proxy or "",
            )
            self.ctx["proxy_profile_id"] = profile_id
            self.ctx["input_protocol"] = 1
            await self.mark("preflight", "success", self.ctx)
        workspace = await self.workspace()
        if workspace is None or workspace.official_workspace_id != self.ctx["official_workspace_id"]:
            return self.stop("manual_required", "workspace_changed", "团队官方身份已变化，已停止，请核对原轮转")
        if (stopped := await self.cancel_point()) is not None:
            return stopped
        for stage in (self.stage_pause, self.stage_kick, self.stage_vacancy, self.stage_onboard,
                      self.stage_publish, self.stage_delete_old, self.stage_count):
            stopped = await stage()
            if stopped is not None:
                return stopped
        if not self.in_test:
            await refresh_after_rotation(self.db, self.op.workspace_id)
        counted = await self.step_result("counted")
        published = await self.step_result("published")
        deleted = await self.step_result("old_remote_deleted")
        account = await self.replacement()
        return self.result(
            success=True, status="success", rotated=True, joined=True, authorized=True, pushed=True,
            message=f"{self.ctx['old_email']} → {self.ctx['replacement_email']} 轮转完成",
            new_account_id=account.id if account else None, new_remote_id=published.get("new_remote_id"),
            old_remote_id=self.ctx.get("old_remote_id") or None, old_remote_deleted=deleted,
            switch_count=counted.get("switch_count"),
        )

    async def reserve_browser(self) -> bool:
        """Take the single browser slot before kicking, so a kicked team never waits in line."""
        if self.in_test or self.session is not None:
            return True
        from app.application.jobs import browser as browser_slot

        other = await self.db.scalar(select(Operation.id).where(
            Operation.state.in_(("running", "waiting")), Operation.op_type.in_(BROWSER_ACTIONS),
            Operation.public_id != self.public_id,
        ).limit(1))
        session = browser_slot.InvitedBrowserSession()
        if other is not None or not await session.try_reserve():
            await session.close()
            return False
        self.session = session
        return True

    async def release_browser(self) -> None:
        if self.session is not None:
            session, self.session = self.session, None
            await session.close()

    # -- stages

    async def stage_pause(self) -> dict[str, Any] | None:
        if await self.done("paused"):
            return None
        if not await self.reserve_browser():
            return self.stop("failed", "browser_busy", "浏览器正被其他任务使用，未暂停、未移出任何账号，请稍后重新发起")
        remote_id = str(self.ctx.get("old_remote_id") or "")
        if not remote_id:
            await self.mark("paused", "success", {"skipped": True, "reason": "no_old_remote"})
            return None
        old_account = await self.db.get(Account, int(self.ctx["old_account_id"]))
        binding = await self.rotate._remote_binding_for(self.db, old_account, workspace_id=self.op.workspace_id)
        if binding.get("state") != "matched" or str(binding.get("remote_id")) != remote_id:
            await self.mark("paused", "failed", {"binding": binding.get("state")}, code="old_binding_changed",
                            error="旧号远端绑定在预检后发生变化")
            return self.stop("failed", "old_binding_changed", "旧号的 Sub2API 绑定在预检后发生变化，未暂停、未移出")
        if sub2api_status.source_signature(await self.rotate.sub2api.load_config(self.db)) != self.ctx["sub2api_source"]:
            return self.stop("manual_required", "sub2api_instance_changed", "Sub2API 连接已变化，未暂停或移出，请人工核对")
        await self.mark("paused", "running", {"intent": "pause", "remote_id": remote_id})
        paused = await self.rotate._pause_and_drain(
            self.db, job_id=self.public_id, remote_id=int(remote_id),
            drain_seconds=0 if self.in_test else 2, in_test=False,
        )
        if not paused.get("ok"):
            # The PATCH may or may not have landed; the old member is still in the team.
            return self.stop("manual_required", "pause_failed",
                             f"旧号调度暂停未确认（{paused.get('error') or '未知原因'}），旧号仍在团队；核对后可继续")
        return None

    async def stage_kick(self) -> dict[str, Any] | None:
        if await self.done("kicked"):
            return None
        if (stopped := await self.cancel_point()) is not None:
            return stopped
        if not await self.reserve_browser():
            return await self.touched("browser_busy", "旧号仍在团队，浏览器正被其他任务使用；稍后继续本轮")
        workspace = await self.workspace()
        live, member = await self.rotate.workspaces.lookup_live_member(self.db, workspace, self.ctx["old_email"])
        if not live.get("success"):
            return await self.touched("member_lookup_unknown", "移出前无法确认官方成员状态，请核对后继续", manual=True)
        if member and (str(member.get("user_id") or "") != str(self.ctx.get("old_user_id") or "")
                       or normalize_official_role(member.get("role")) != self.ctx["role"]
                       or member.get("seat_type") != self.ctx["official_seat_type"]):
            return await self.touched("old_member_changed", "旧成员身份、角色或席位已变化，未移出，请人工核对", manual=True)
        await self.mark("kicked", "running", {"intent": "remove_member"})
        await self.note("kicking", "正在移出旧成员（Sub2API 旧账号只暂停，不删除）")
        # kick_to_standby re-reads the member first, so a lost DELETE response is never blindly repeated.
        kicked = await self.rotate.kick_to_standby(
            self.db, workspace_id=self.op.workspace_id, email=self.ctx["old_email"],
            reason="manual_rotation", job_id=self.public_id, in_test=self.in_test, keep_remote=True,
        )
        await self.fresh()
        if not kicked.get("success") and not await self.done("official_removed"):
            if kicked.get("status") == "cancelled":
                return await self.cancel_point() or self.stop("cancelled", "cancelled", "已取消")
            code = str(kicked.get("error_code") or "kick_failed")
            await self.mark("kicked", "failed", {"error_code": code}, code=code, error=str(kicked.get("error") or ""))
            unknown = kicked.get("status") in {"manual_required", "awaiting_confirmation", "partial", "state_changed"}
            return await self.touched(code, f"{kicked.get('error') or '移出旧成员失败'}；请先同步团队核对，继续时会先读取官方状态",
                                      manual=unknown or code in {"kick_unverified", "write_outcome_unknown"})
        await self.mark("kicked", "success", {
            "vacancy": kicked.get("vacancy"), "already_absent": bool(kicked.get("already_absent")),
            "warning": kicked.get("error") if not kicked.get("success") else None,
        })
        return None

    async def stage_vacancy(self) -> dict[str, Any] | None:
        if await self.done("vacancy"):
            return None
        vacancy = (await self.step_result("kicked")).get("vacancy")
        if is_safe_to_refill(vacancy):
            await self.mark("vacancy", "success", {"confirmed_by": "official_receipt"})
        elif self.confirm_vacancy:
            await self.mark("vacancy", "success", {"confirmed_by": "operator"})
        elif not is_billable_vacancy(vacancy):
            # Manual one-for-one swap: only an explicit billing signal stops the refill.
            await self.mark("vacancy", "success", {"confirmed_by": "no_billing_signal", "vacancy": vacancy})
        else:
            await self.mark("vacancy", "failed", {"vacancy": vacancy}, code="vacancy_not_safe_to_refill",
                            error="移出回执显示空出的席位可能收费")
            return await self.touched("vacancy_not_safe_to_refill",
                                      "旧号已移出，但回执显示空出的席位可能收费；核对账单后继续并确认补位", manual=True)
        return None

    async def _authorized_outside(self) -> Account | None:
        """The new account was already authorized elsewhere (e.g. by hand after a phone check)."""
        account, workspace = await self.replacement(), await self.workspace()
        if account is None or workspace is None:
            return None
        if (account.auth_state != "healthy" or not decrypt_secret(account.refresh_token_encrypted)
                or not decrypt_secret(account.access_token_encrypted)):
            return None
        selected = jwt_parser.extract_chatgpt_account_id(decrypt_secret(account.access_token_encrypted))
        if not selected or token_workspace_mismatch(account, workspace):
            return None
        live, member = await self.rotate.workspaces.lookup_live_member(self.db, workspace, account.email)
        if (not live.get("success") or not member or member.get("status") != "joined"
                or normalize_official_role(member.get("role")) != self.ctx["role"]
                or member.get("seat_type") != self.ctx["official_seat_type"]):
            return None
        return account

    async def stage_onboard(self) -> dict[str, Any] | None:
        if await self.done("authorized"):
            return None
        if (account := await self._authorized_outside()) is not None:
            if not await self.done("joined"):
                await self.mark("joined", "success", {"account_id": account.id})
            await self.mark("authorized", "success", {
                "account_id": account.id, "credential_revision": int(account.credential_revision or 1),
                "authorized_outside": True,
            })
            await self.note("authorized", "新号已在外部完成授权，跳过浏览器")
            return None
        if not await self.reserve_browser():
            return await self.touched("browser_busy", "旧号已移出，浏览器正被其他任务使用；稍后继续本轮")
        if (stopped := await self.cancel_point()) is not None:
            return stopped
        await self.note("inviting", f"正在邀请 {self.ctx['replacement_email']}（继承 {self.ctx['role']} / {self.ctx['seat_intent']}）")
        outcome = await self.rotate.onboard.invite_and_onboard(
            self.db, workspace_id=self.op.workspace_id, email_line=self.ctx["replacement_email"],
            reuse_existing=True, job_id=self.public_id, in_test=self.in_test,
            role=self.ctx["role"], seat_intent=self.ctx["seat_intent"],
            oauth_signup=True, use_phone_pool=False, signup_flow=self.ctx.get("signup_flow") or SIGNUP_FLOW,
            browser_session=self.session, keep_operation_identity=True,
            # Frozen at preflight; tasks from before the runner existed stay on Playwright.
            signup_runner=self.ctx.get("signup_runner") or "playwright",
        )
        await self.db.commit()  # Same as the standalone onboard command: keep what it recorded.
        await self.fresh()
        # Registration and OAuth may leave the browser worker in any state; never reuse it.
        await self.release_browser()
        account = await self.replacement()
        joined = bool(outcome.get("joined") or outcome.get("authorized"))
        if joined and not await self.done("joined"):
            await self.mark("joined", "success", {"account_id": account.id if account else None})
        if outcome.get("success") and outcome.get("authorized") and account is not None:
            await self.mark("authorized", "success", {
                "account_id": account.id, "credential_revision": int(account.credential_revision or 1),
            })
            return None
        code = str(outcome.get("error_code") or ("oauth_failed" if joined else "onboard_failed"))
        manual = any(marker in code for marker in MANUAL_MARKERS)
        await self.mark("authorized", "failed", {"joined": joined, "error_code": code},
                        code=code, error=str(outcome.get("error") or ""))
        where = "新号已入组，授权未完成" if joined else "新号尚未完成注册入组"
        return await self.touched(code, f"{where}（{outcome.get('error') or code}）；继续时只处理同一新邮箱，旧号 Sub2API 记录保留",
                                  manual=manual, joined=joined, authorized=False)

    async def _new_state(self, account: Account, workspace: Workspace) -> tuple[str, str]:
        """Fresh, instance-bound proof that the new account is the right identity and schedulable."""
        authorized = await self.step_result("authorized")
        if workspace.official_workspace_id != self.ctx["official_workspace_id"]:
            return "workspace_changed", "团队官方身份已变化"
        if (account.auth_state != "healthy" or not decrypt_secret(account.refresh_token_encrypted)
                or not decrypt_secret(account.access_token_encrypted)):
            return "new_auth_changed", "新号授权状态已变化"
        if int(account.credential_revision or 1) != int(authorized.get("credential_revision") or 0):
            return "credential_revision_changed", "新号凭据在推送后发生变化"
        live, member = await self.rotate.workspaces.lookup_live_member(self.db, workspace, account.email)
        if (not live.get("success") or not member or member.get("status") != "joined"
                or normalize_official_role(member.get("role")) != self.ctx["role"]
                or member.get("seat_type") != self.ctx["official_seat_type"]):
            return "new_membership_changed", "新号官方成员、角色或席位未确认"
        selected = jwt_parser.extract_chatgpt_account_id(decrypt_secret(account.access_token_encrypted))
        if not selected or not workspace.official_workspace_id:
            return "workspace_unconfirmed", "新号授权凭据缺少可核对的工作空间身份"
        if token_workspace_mismatch(account, workspace):
            return "workspace_mismatch", "新号授权选择的工作空间不是该团队"
        if sub2api_status.source_signature(await self.rotate.sub2api.load_config(self.db)) != self.ctx.get("sub2api_source"):
            return "sub2api_instance_changed", "Sub2API 连接在轮转中发生变化"
        binding = await self.rotate._remote_binding_for(self.db, account, workspace_id=workspace.id)
        if binding.get("state") != "matched":
            return "new_binding_unverified", "新号的 Sub2API 绑定尚未核对通过"
        remote_id = str(binding.get("remote_id") or "")
        if remote_id == str(self.ctx.get("old_remote_id") or ""):
            return "remote_id_collision", "新旧账号指向同一个 Sub2API 账号"
        try:
            remote = await self.rotate.sub2api.get_account(self.db, int(remote_id))
        except Exception:  # noqa: BLE001
            return "new_readback_failed", "读取新号 Sub2API 状态失败"
        if validate_sync_identity(remote, remote_id, account.email, workspace.official_workspace_id):
            return "new_identity_unconfirmed", "新号远端身份与团队不一致"
        if sub2api_status.classify(remote) != "healthy":
            return "new_not_schedulable", "新号远端尚未处于可调度状态"
        return "", remote_id

    async def stage_publish(self) -> dict[str, Any] | None:
        if await self.done("published"):
            return None
        if (stopped := await self.cancel_point()) is not None:
            return stopped
        account, workspace = await self.replacement(), await self.workspace()
        if sub2api_status.source_signature(await self.rotate.sub2api.load_config(self.db)) != self.ctx["sub2api_source"]:
            return await self.touched("sub2api_instance_changed", "Sub2API 连接已变化，未推送或删除，请人工核对", manual=True)
        previous = await self.step_result("published")
        if (previous.get("intent") == "publish" and previous.get("sub2api_operation_id")
                and ("publish_written" not in previous or previous.get("publish_outcome_unknown"))):
            # A process may die after the write but before the root gets its receipt.
            # Reconcile this operation id; never issue another credential write.
            previous["publish_written"] = True
        if int(account.credential_revision or 1) != int((await self.step_result("authorized")).get("credential_revision") or 0):
            return await self.touched("credential_revision_changed", "新号凭据版本已变化，未覆盖远端凭据，请核对", manual=True)
        if not previous.get("intent"):
            # Already pushed by hand after a manual authorization: verify, do not write again.
            code, detail = await self._new_state(account, workspace)
            if not code:
                await self.mark("published", "success", {"new_remote_id": detail, "publish_written": True,
                                                         "already_published": True,
                                                         "credential_revision": int(account.credential_revision or 1)})
                return None
        await self.mark("published", "running", previous)
        invite = {
            "authorized": True, "child": {"id": account.id, "email": account.email},
            "rotation_operation_id": self.public_id,
            "publish_written": bool(previous.get("publish_written")),
            "publish_receipt_ok": bool(previous.get("publish_receipt_ok")),
            "sub2api_operation_id": previous.get("sub2api_operation_id"),
        }
        await self.note("sub2api_push", "新号已授权，正在推送并回读 Sub2API")
        published = await publish_replacement(self.db, invite, workspace.id)
        await self.fresh()
        progress = {key: published.get(key) for key in ("publish_written", "publish_receipt_ok", "publish_outcome_unknown", "sub2api_operation_id")}
        persisted = await self.step_result("published")
        progress = {**previous, **persisted, **{k: v for k, v in progress.items() if v is not None}}
        if not published.get("success"):
            await self.mark("published", "failed", progress, code="publish_pending", error="Sub2API 尚未确认新号可用")
            return await self.touched("publish_pending", "新号已授权，Sub2API 尚未确认可用；旧号远端记录保留，继续时只重试推送核对")
        await self.db.refresh(account)
        code, detail = await self._new_state(account, workspace)
        if code:
            await self.mark("published", "failed", {**progress, "check": code}, code=code, error=detail)
            return await self.touched(code, f"{detail}；未删除旧号远端记录", manual=True)
        await self.mark("published", "success", {**progress, "new_remote_id": detail, "publish_written": True,
                                                 "credential_revision": int(account.credential_revision or 1)})
        return None

    async def _old_remote_absent(self, remote_id: int) -> bool | None:
        """Use the complete authenticated catalog; a route-level 404 proves nothing."""
        try:
            remotes = await self.rotate.sub2api.list_status_accounts(self.db)
        except Exception:
            return None
        if not isinstance(remotes, list) or any(not isinstance(row, dict) or not str(row.get("id", "")).isdigit() for row in remotes):
            return None
        return not any(str(row["id"]) == str(remote_id) for row in remotes)

    async def stage_delete_old(self) -> dict[str, Any] | None:
        if await self.done("old_remote_deleted"):
            return None
        remote_id = str(self.ctx.get("old_remote_id") or "")
        if not remote_id:
            await self.mark("old_remote_deleted", "success", {"skipped": True, "reason": "no_old_remote"})
            return None
        if (stopped := await self.cancel_point()) is not None:
            return stopped
        account, workspace = await self.replacement(), await self.workspace()
        published = await self.step_result("published")
        code, detail = await self._new_state(account, workspace)
        if code or detail != str(published.get("new_remote_id")):
            await self.mark("old_remote_deleted", "failed", {"check": code or "new_remote_changed"},
                            code=code or "new_remote_changed", error=detail)
            return await self.touched(code or "new_remote_changed", "删除旧号前复核新号未通过，旧号远端记录保留", manual=True)
        old_account = await self.db.get(Account, int(self.ctx["old_account_id"]))
        if old_account is None or await has_other_active_context(self.db, old_account, workspace.id):
            return await self.touched("old_other_context", "旧号关联其他团队，未删除旧远端账号", manual=True)
        binding = await self.db.get(ExternalBinding, int(self.ctx["old_binding_id"]))
        if (binding is None or binding.binding_state != "verified" or binding.provider != "sub2api"
                or str(binding.remote_account_id) != remote_id
                or binding.local_account_id != int(self.ctx["old_account_id"]) or binding.workspace_id != workspace.id):
            await self.mark("old_remote_deleted", "failed", {"check": "old_binding_changed"}, code="old_binding_changed",
                            error="旧号本地绑定已变化")
            return await self.touched("old_binding_changed", "新号可用；旧号本地绑定已变化，未删除任何远端账号，请人工核对", manual=True)
        absent = await self._old_remote_absent(int(remote_id))
        if absent is False:
            try:
                remote = await self.rotate.sub2api.get_account(self.db, int(remote_id))
            except Exception:  # noqa: BLE001
                remote = None
            if validate_sync_identity(remote, remote_id, self.ctx["old_email"], workspace.official_workspace_id):
                await self.mark("old_remote_deleted", "failed", {"check": "old_identity"}, code="old_identity_unconfirmed",
                                error="旧号远端身份与冻结记录不一致")
                return await self.touched("old_identity_unconfirmed", "新号可用；旧号远端身份核对不一致，未删除", manual=True)
            if (stopped := await self.cancel_point()) is not None:
                return stopped
            # Refresh the revenue entry booked at kick time while the remote still exists.
            await revenue_ledger.settle_binding(self.db, binding, source="manual_rotation", operation_id=self.public_id)
            await self.mark("old_remote_deleted", "running", {"intent": "delete", "remote_id": remote_id})
            receipt = None
            try:
                receipt = await self.rotate.sub2api.delete_accounts(self.db, [int(remote_id)])
            except Exception:  # noqa: BLE001
                logger.warning("old remote delete request failed operation_id=%s", self.public_id)
            await self.fresh()
            absent = await self._old_remote_absent(int(remote_id))
            deleted = bool(receipt and remote_id in {str(v) for v in receipt.get("deleted") or []})
            if absent is not True:
                await self.mark("old_remote_deleted", "failed", {"receipt": deleted, "absent": absent},
                                code="old_remote_delete_pending", error="旧号远端删除未确认")
                return await self.touched("old_remote_delete_pending",
                                          "新号已可用，旧号 Sub2API 下架待处理；继续时只会核对并清理旧号", new_available=True)
        elif absent is None:
            await self.mark("old_remote_deleted", "failed", {"absent": None}, code="old_remote_unknown",
                            error="无法确认旧号远端状态")
            return await self.touched("old_remote_unknown", "新号已可用，暂时无法读取旧号 Sub2API 状态；继续时只处理旧号清理",
                                      new_available=True)
        # Confirmed gone from the same instance: drop only this old binding, keep the account record.
        await self.db.delete(binding)
        await self.mark("old_remote_deleted", "success", {"remote_id": remote_id, "confirmed_absent": True})
        return None

    async def stage_count(self) -> dict[str, Any] | None:
        if await self.done("counted"):
            return None
        account = await self.replacement()
        counted = await increment_workspace_switch_count(self.db, int(self.op.workspace_id), commit=False)
        if not counted.get("ok"):
            await self.db.rollback()
            return await self.touched("count_failed", "轮转已完成，但团队计数未写入；继续时只补计数")
        membership = await self.db.scalar(select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == self.op.workspace_id,
            WorkspaceMembership.account_id == account.id,
            WorkspaceMembership.membership_state == MEMBERSHIP_STATE_JOINED,
        ))
        if membership is not None and membership.switch_counted_at is None:
            # The extension's per-membership counter must not add this rotation again.
            membership.switch_counted_at = utcnow()
        # Counter, dedupe marker and step record commit together.
        await self.mark("counted", "success", {"switch_count": counted["switch_count"]})
        return None


async def run_manual_rotation(db, public_id: str, *, confirm_vacancy: bool = False, rotate=None, in_test: bool = False) -> dict[str, Any]:
    op = await operation_store.get_by_public_id(db, public_id)
    if op is None:
        return {"success": False, "error_code": "not_found", "error": "operation not found"}
    return await _Rotation(db, op, rotate or _default_rotate(), confirm_vacancy=confirm_vacancy, in_test=in_test).run()
