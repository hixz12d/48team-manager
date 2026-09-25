"""After-authorization follow-ups and the signup extension's sync -> link -> OAuth handoff.

The follow-ups are the two chores that used to be done by hand after a member was
authorized: push the account to Sub2API and add one to the team's daily switch counter.
The counter is idempotent per membership, so retries or re-authorizations never double count.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.operations import operation_store
from app.application.tokens import decrypt_secret
from app.application.workspace_switch_count import increment_workspace_switch_count, switch_count_record
from app.core.jwt import jwt_parser
from app.core.time import utcnow
from app.domain.automation import ACTIVE_STATES
from app.domain.identity import MEMBERSHIP_STATE_JOINED
from app.domain.identity.ids import normalize_email, workspace_official_id
from app.domain.identity.policy import is_workspace_owner
from app.domain.workspaces.names import resolve_display_name
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot

UNAVAILABLE_WORKSPACE_STATES = {"archived", "disabled"}


def _step(ok: bool | None, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": ok, "message": message, **extra}


async def _joined_membership(db: AsyncSession, workspace_id: int, account_id: int) -> WorkspaceMembership | None:
    return await db.scalar(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.account_id == account_id,
        WorkspaceMembership.membership_state == MEMBERSHIP_STATE_JOINED,
    ))


def token_workspace_mismatch(account: Account, workspace: Workspace | None) -> bool:
    """True only when the new token clearly names a different workspace than the team."""
    if workspace is None:
        return False
    expected = workspace_official_id(workspace.official_workspace_id)
    selected = workspace_official_id(jwt_parser.extract_chatgpt_account_id(decrypt_secret(account.access_token_encrypted)) or "")
    return bool(expected and selected and expected != selected)


async def count_switch_once(db: AsyncSession, workspace_id: int, account_id: int) -> dict[str, Any]:
    """Add one to today's switch count the first time this membership is authorized."""
    marked = await db.execute(
        update(WorkspaceMembership)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.account_id == account_id,
            WorkspaceMembership.membership_state == MEMBERSHIP_STATE_JOINED,
            WorkspaceMembership.switch_counted_at.is_(None),
        )
        .values(switch_counted_at=utcnow())
        .execution_options(synchronize_session=False)
    )
    if marked.rowcount != 1:
        await db.rollback()
        workspace = await db.get(Workspace, workspace_id)
        member = await _joined_membership(db, workspace_id, account_id)
        record = switch_count_record(workspace) if workspace is not None else None
        if member is None:
            return _step(None, "账号不是该团队的已加入成员，未计数", counted=False, switch_count=record)
        return _step(True, f"该成员已计过切换次数，今日仍为 {record['count']} 次", counted=False, switch_count=record)
    # The membership mark and the counter increment commit together.
    result = await increment_workspace_switch_count(db, workspace_id)
    if not result.get("ok"):
        await db.rollback()
        return _step(False, "团队不存在，未计数", counted=False)
    count = result["switch_count"]["count"]
    return _step(True, f"今日切换 +1，现为 {count} 次", counted=True, switch_count=result["switch_count"])


async def finish_after_authorization(
    db: AsyncSession,
    account_id: int,
    *,
    workspace_id: int | None,
    token_sync: dict[str, Any] | None = None,
    push_sub2api: bool = False,
    count_switch: bool = False,
) -> dict[str, Any]:
    """Run the requested follow-ups once authorization has succeeded. Never raises."""
    if not push_sub2api and not count_switch:
        return {}
    account = await db.get(Account, int(account_id))
    if account is None:
        return {}
    await db.refresh(account)
    workspace = await db.get(Workspace, int(workspace_id)) if workspace_id else None
    owner = str(account.local_purpose or "") == "mother" or (workspace is not None and is_workspace_owner(workspace, account.id))
    member = await _joined_membership(db, workspace.id, account.id) if workspace is not None else None
    mismatch = token_workspace_mismatch(account, workspace)
    followups: dict[str, Any] = {}

    def blocked() -> dict[str, Any] | None:
        if owner:
            return _step(None, "母号不自动推送或计数")
        if workspace is not None and member is None:
            return _step(None, "账号不是该团队的已加入成员，已跳过")
        if mismatch:
            return _step(False, "授权时选择的工作空间不是该团队，已跳过；请重新授权并选择这个团队", error_code="workspace_mismatch")
        return None

    if push_sub2api:
        skip = blocked()
        sync = token_sync or {}
        if skip is not None:
            followups["sub2api"] = skip
        elif sync.get("outcome") == "synced":
            followups["sub2api"] = _step(True, "已更新已绑定的 Sub2API 账号凭据", skipped=True)
        elif sync and not sync.get("ok") and not sync.get("skipped"):
            followups["sub2api"] = _step(False, "已绑定的 Sub2API 账号凭据同步失败，未重复推送，请到账号页核对")
        else:
            from app.application.sub2api_publish import account_sub2api_push
            try:
                pushed = await account_sub2api_push(db, account.id, workspace_id=workspace.id if workspace else None)
            except Exception as exc:  # noqa: BLE001 - the authorization already succeeded
                await db.rollback()
                pushed = {"ok": False, "message": f"推送失败：{exc}"}
            ok = bool(pushed.get("ok") or pushed.get("success"))
            followups["sub2api"] = _step(
                ok,
                str(pushed.get("message") or ("Sub2API 推送完成" if ok else pushed.get("error") or "Sub2API 推送失败")),
                operation_id=pushed.get("operation_id"),
                status=pushed.get("status"),
            )

    if count_switch:
        skip = blocked()
        if workspace is None:
            followups["switch_count"] = _step(None, "未指定团队，未计数")
        elif skip is not None:
            followups["switch_count"] = skip
        else:
            followups["switch_count"] = await count_switch_once(db, workspace.id, account.id)
    return followups


def _workspace_names(workspace: Workspace, owner_email: str | None = None) -> list[str]:
    names = [resolve_display_name(workspace, owner_email=owner_email)["display_name"],
             workspace.official_name, workspace.name]
    seen: list[str] = []
    for name in names:
        text = str(name or "").strip()
        if text and text.lower() not in {item.lower() for item in seen}:
            seen.append(text)
    return seen


async def list_extension_workspaces(db: AsyncSession) -> list[dict[str, Any]]:
    rows = list((await db.scalars(select(Workspace).where(Workspace.status.not_in(tuple(UNAVAILABLE_WORKSPACE_STATES))).order_by(Workspace.id))).all())
    items = []
    for workspace in rows:
        owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
        if owner is None or not workspace.official_workspace_id:
            continue
        names = _workspace_names(workspace, owner.email)
        items.append({
            "id": workspace.id,
            "name": names[0],
            "names": names,
            "seat_limit": workspace.seat_limit,
            "occupied_seats": workspace.occupied_seats,
            "switch_count": switch_count_record(workspace),
        })
    return items


def _handoff_error(error_code: str, message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "state": "failed", "error_code": error_code, "message": message, **extra}


async def extension_handoff(
    db: AsyncSession,
    *,
    email: str,
    workspace_id: int,
    sync_operation_id: str | None = None,
) -> dict[str, Any]:
    """One short step of: sync the team, link the joined member, start OAuth.

    Without ``sync_operation_id`` a (deduplicated) team sync is queued and ``syncing`` is
    returned. The caller polls with the returned id until the sync finishes; then the
    member is linked if needed and a manual OAuth session is created for it.
    """
    from app.application.jobs.workspace_sync import enqueue_workspace_sync
    from app.application.console_maintenance import link_remote_only_member
    from app.application.reauth import reauth_service
    from app.application.oauth_sessions import OAuthSessionError

    target = normalize_email(email)
    if not target or "@" not in target:
        return _handoff_error("email_required", "邮箱格式不正确")
    workspace = await db.get(Workspace, int(workspace_id))
    if workspace is None or workspace.status in UNAVAILABLE_WORKSPACE_STATES:
        return _handoff_error("workspace_unavailable", "团队不存在或已停用")

    if not sync_operation_id:
        queued = await enqueue_workspace_sync(db, workspace.id, source="extension")
        if not queued.get("ok"):
            return _handoff_error(str(queued.get("error_code") or "sync_failed"), "无法同步该团队：母号缺少授权或团队不可用")
        return {"ok": True, "state": "syncing", "operation_id": queued["operation_id"], "message": "正在同步团队成员"}

    operation = await operation_store.get_by_public_id(db, sync_operation_id)
    if operation is None or operation.op_type != "workspace_sync" or operation.workspace_id != workspace.id:
        return _handoff_error("sync_operation_invalid", "同步任务无效，请重新开始")
    if operation.state in ACTIVE_STATES:
        return {"ok": True, "state": "syncing", "operation_id": operation.public_id, "message": "正在同步团队成员"}
    if operation.state != "success":
        return _handoff_error("sync_failed", "团队同步失败，请到控制台检查母号授权后重试")

    snapshot = await db.scalar(select(WorkspaceOfficialMemberSnapshot).where(
        WorkspaceOfficialMemberSnapshot.workspace_id == workspace.id,
        WorkspaceOfficialMemberSnapshot.normalized_email == target,
    ))
    if snapshot is None:
        return {"ok": False, "state": "not_found", "error_code": "member_not_found",
                "message": "团队成员列表里没有这个邮箱，请确认已发出邀请且邀请的是这个邮箱"}
    if snapshot.remote_state == "invited":
        return {"ok": False, "state": "not_joined", "error_code": "member_not_joined",
                "message": "该邮箱在团队中仍是「已邀请」，请在 ChatGPT 页面接受邀请后重试"}
    if snapshot.remote_state != "joined":
        return {"ok": False, "state": "not_found", "error_code": "member_not_joined",
                "message": "该邮箱目前不是团队成员"}

    account = await db.scalar(select(Account).where(Account.email == target))
    if account is not None and (is_workspace_owner(workspace, account.id) or str(account.local_purpose or "") == "mother"):
        return _handoff_error("owner_account", "这是母号，插件不处理母号授权")
    if account is None or await _joined_membership(db, workspace.id, account.id) is None:
        linked = await link_remote_only_member(db, workspace.id, email=target, account_id=account.id if account else None)
        if not linked.get("ok") and linked.get("error_code") != "already_linked":
            return _handoff_error(str(linked.get("error_code") or "link_failed"), str(linked.get("error") or "接入本地失败"))
        account = await db.get(Account, int(linked["account_id"]))
    if account is None:
        return _handoff_error("account_not_found", "接入后未找到本地账号")
    try:
        started = await reauth_service.start_manual_reauth(db, account)
    except OAuthSessionError as exc:
        await db.rollback()
        return _handoff_error(exc.error_code, str(exc))
    if not started.get("ok") or not started.get("authorize_url"):
        return _handoff_error(str(started.get("error_code") or "oauth_start_failed"), str(started.get("error") or "生成授权链接失败"))
    owner = await db.get(Account, workspace.owner_account_id) if workspace.owner_account_id else None
    names = _workspace_names(workspace, owner.email if owner else None)
    return {
        "ok": True,
        "state": "authorize",
        "account_id": account.id,
        "email": account.email,
        "ticket": started["ticket"],
        "authorize_url": started["authorize_url"],
        "workspace": {"id": workspace.id, "name": names[0], "names": names},
        "message": "授权链接已生成",
    }
