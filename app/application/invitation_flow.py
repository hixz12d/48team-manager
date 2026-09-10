"""Preflight and completion for invitation-first, one-account onboarding."""
from __future__ import annotations

from sqlalchemy import select

from app.application.tokens import auth_service, decrypt_secret
from app.application.reauth import load_cf_config
from app.domain.identity.ids import normalize_email
from app.integrations.mail.otp import parse_mail_line
from app.integrations.openai.member_adapter import (
    existing_invite_seat_error, official_roles_equivalent, parse_invite_role,
    parse_invite_seat_intent,
)
from app.integrations.sms.client import parse_optional_sms
from app.persistence.models.identity import Account, WorkspaceMembership


def failed(code, message, **fields):
    return {"success": False, "error_code": code, "error": message, **fields}


async def browser_progress(db, job_id, stage):
    if not job_id:
        return
    from app.application.operations import operation_store

    op = await operation_store.get_by_public_id(db, job_id)
    if op is None:
        return
    await db.refresh(op)
    if op.cancel_requested:
        import asyncio
        raise asyncio.CancelledError()
    if stage == "heartbeat":
        await operation_store.heartbeat(db, op)
    else:
        await operation_store.note(db, op, stage, f"浏览器阶段：{stage}")
    await db.commit()


async def prepare(service, db, *, workspace_id, email_line, phone_line, role, seat_intent, skip_invite):
    """Validate before claiming HME; resume unfinished children before choosing standby."""
    try:
        parse_optional_sms(phone_line)
        parse_invite_role(role)
        parse_invite_seat_intent(seat_intent)
    except ValueError:
        return email_line, failed("invalid_onboard_input", "角色、席位或接码参数无效")
    if skip_invite:
        return email_line, failed("invite_required", "邀请注册流程不能跳过邀请")
    workspace = await service._load_workspace(db, workspace_id)
    if workspace is None or workspace.status != "active":
        return email_line, failed("workspace_unavailable", "团队不存在或已停用")
    owner = workspace.owner_account
    if owner is None or not owner.proxy:
        return email_line, failed("proxy_missing", "母号尚未配置代理")
    if not email_line.strip():
        from sqlalchemy import or_
        from app.persistence.models.operations import Operation

        member_ids = select(WorkspaceMembership.account_id).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.membership_state.in_(("invited", "joined")),
        )
        unfinished_ids = select(Operation.account_id).where(
            Operation.workspace_id == workspace_id,
            Operation.op_type.in_(("onboard", "replenish")),
            Operation.state.in_(("failed", "partial", "manual_required", "cancelled")),
        )
        pending = list((await db.execute(
            select(Account).where(
                or_(Account.id.in_(member_ids), Account.id.in_(unfinished_ids)),
                Account.local_purpose == "child",
                Account.operational_state.notin_(("disabled", "archived", "standby")),
            ).order_by(Account.id)
        )).scalars().unique())
        pending = [a for a in pending if not decrypt_secret(a.refresh_token_encrypted) or a.auth_state in {"oauth_required", "manual_required"}]
        if len(pending) > 1:
            return email_line, failed("resume_account_required", "存在多个未完成子号，请指定邮箱继续")
        candidate = pending[0] if pending else await service.pick_replacement(db)
        if candidate:
            email_line = candidate.mail_raw or candidate.email
    parsed = parse_mail_line(email_line)
    target = normalize_email(parsed.get("email"))
    if target == normalize_email(owner.email):
        return email_line, failed("primary_mother_protected", "不能把母号作为子号注册")
    if target:
        account = await db.scalar(select(Account).where(Account.email == target))
        if account and (account.local_purpose == "mother" or account.operational_state in {"disabled", "archived"}):
            return email_line, failed("account_unavailable", "所选账号不能作为子号注册")
        if account and account.mail_raw and not parsed.get("pickup_url"):
            email_line = account.mail_raw
            parsed = parse_mail_line(email_line)
    cf = await load_cf_config(db)
    if not parsed.get("pickup_url") and not all(cf.values()):
        return email_line, failed("mail_missing", "请先配置邮箱验证码和邀请邮件读取方式")
    live, item = await service.workspaces.lookup_live_member(db, workspace, target)
    if not live.get("success") or live.get("lookup_state") == "unknown_due_to_error":
        return email_line, failed("invite_lookup_unknown", "无法确认官方成员和邀请，未领取 HME")
    if not item and workspace.seat_limit is not None and len(live.get("members") or []) >= workspace.seat_limit:
        return email_line, failed("team_full", "团队席位已满，未领取 HME 或发送邀请")
    if not target and any(m.get("status") == "invited" for m in live.get("members") or []):
        return email_line, failed("resume_account_required", "官方仍有待接受邀请，请指定该邮箱继续")
    return email_line, None


async def authorize_joined(service, db, result, *, workspace_id, phone_line, role, seat_intent, job_id, executable_path):
    """Only the live membership gate can unlock OAuth; never register in OAuth."""
    from app.application.oauth_signup import run_invited_oauth_signup

    child = await db.get(Account, result["child"]["id"])
    workspace = await service._load_workspace(db, workspace_id)
    base = {**result, "joined": True, "pushed": False}
    try:
        member = await service._confirm_joined(db, workspace, child.email)
        if not member:
            return {**base, **failed("not_joined", "官方尚未确认入组，未启动授权", joined=False)}
        seat_error = existing_invite_seat_error(parse_invite_seat_intent(seat_intent), member.get("seat_type"))
        if seat_error or not official_roles_equivalent(member.get("role"), parse_invite_role(role)):
            return {**base, **failed("membership_mismatch", "官方角色或席位与请求不一致，未启动授权")}
        if result.get("skipped") and decrypt_secret(child.refresh_token_encrypted) and child.auth_state == "healthy":
            return {**base, "authorized": True}
        await service._progress(db, job_id=job_id, stage="authorizing", message="已确认入组，正在进行 OAuth 授权")
        parsed = parse_mail_line(child.mail_raw or child.email)
        cf = await load_cf_config(db)
        outcome = await run_invited_oauth_signup(
            db, child=child, workspace=workspace, password=decrypt_secret(child.password_encrypted),
            pickup_url=parsed.get("pickup_url") or "", use_cloudflare=not parsed.get("pickup_url"),
            cf_config=cf, job_id=job_id, executable_path=executable_path, phone_line=phone_line,
        )
        if not outcome.get("ok"):
            child.auth_state = "oauth_required"
            await db.commit()
            return {**base, **failed(outcome.get("error_code") or "oauth_failed", outcome.get("error") or "授权失败"),
                    "partial": True, "status": "partial", "authorized": False,
                    "message": f"{child.email} 已入组，授权未完成。请继续此邮箱，不要再领新号。"}
        await auth_service.apply_tokens(child, outcome)
        child.auth_state = "healthy"
        if outcome.get("sms_verified") and outcome.get("phone"):
            child.phone = outcome["phone"]
        await db.commit()
        current = await service._confirm_joined(db, workspace, child.email)
        if not current or existing_invite_seat_error(parse_invite_seat_intent(seat_intent), current.get("seat_type")) or not official_roles_equivalent(current.get("role"), parse_invite_role(role)):
            return {**base, **failed("membership_changed", "授权凭据已保存，但最终成员角色或席位未确认"),
                    "partial": True, "authorized": True, "joined": bool(current)}
        return {**base, "authorized": True, "skipped": False, "message": f"{child.email} 已入组并完成授权，未推送 Sub2API"}
    except Exception:
        await db.rollback()
        return {**base, **failed("oauth_failed", "授权未完成，请使用同一邮箱继续"), "partial": True, "authorized": False}
