"""Serial mailbox OAuth recovery for an existing codex-rs binding, never a new account."""
from __future__ import annotations

import asyncio
from datetime import timedelta

from sqlalchemy import select, update

from app.application import codex_status
from app.application.jobs.browser import InvitedBrowserSession
from app.application.jobs.commands import command_runner
from app.application.oauth_sessions import oauth_session_store, OAuthSessionError
from app.application.operations import operation_store, unpack_input
from app.application.reauth import reauth_service, load_cf_config
from app.application.resources.hme import load_config as load_hme_config
from app.application.tokens import auth_service
from app.core.time import as_utc, utcnow
from app.domain.automation import ACTIVE_STATES
from app.integrations.codex.client import CodexClient, load_config
from app.integrations.openai import oauth_sessions
from app.integrations.openai.chatgpt import ChatGPTClient, chatgpt_client
from app.persistence.models.codex import CodexBinding
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership, ExternalBinding
from app.persistence.models.oauth import OAuthSession
from app.persistence.models.operations import Operation

_LOCK = asyncio.Lock()
MAX_ATTEMPTS = 3
MANUAL_CODES = {"phone_verification_required", "cloudflare_challenge", "account_deactivated",
                "account_not_registered", "password_missing", "password_rejected", "identity_mismatch"}


class RecoveryStopped(ValueError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def fail(code, message, *, manual=True):
    return {"ok": False, "success": False, "status": "manual_required" if manual else "failed",
            "error_code": code, "error": message}


async def context(db, account_id):
    account = await db.get(Account, account_id, populate_existing=True)
    binding = await db.get(CodexBinding, account_id, populate_existing=True)
    if (account is None or binding is None or not binding.remote_account_id
            or not binding.import_attempted or binding.state not in {"synced", "uncertain"}
            or account.local_purpose != "child" or account.operational_state in {"disabled", "archived"}):
        raise RecoveryStopped("not_eligible", "仅恢复仍在团队中的 rs 托管子号")
    from app.application.refresh_ownership import remote_refresh_owner
    if await remote_refresh_owner(db, account_id) or await db.scalar(select(ExternalBinding.id).where(
            ExternalBinding.local_account_id == account_id, ExternalBinding.provider == "sub2api",
            ExternalBinding.binding_state.not_in(("missing", "orphaned"))).limit(1)):
        raise RecoveryStopped("sub2api_bound", "存在 Sub2API 绑定或刷新委托，停止 rs 自动重授权")
    if await db.scalar(select(Workspace.id).where(Workspace.owner_account_id == account_id).limit(1)):
        raise RecoveryStopped("owner_manual", "母号不参与自动重授权")
    workspace = await db.scalar(select(Workspace).join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
        .where(WorkspaceMembership.account_id == account_id, WorkspaceMembership.membership_state == "joined",
               Workspace.status == "active", Workspace.official_workspace_id == binding.official_workspace_id).limit(1))
    if workspace is None:
        raise RecoveryStopped("membership_changed", "原团队关系不存在或已停用，停止重授权")
    cfg = await load_config(db)
    if cfg["base_url"] != binding.target_url:
        raise RecoveryStopped("target_changed", "rs 连接已改变，停止更新旧绑定")
    return account, binding, workspace, CodexClient(**cfg)


async def verified_remote(db, account_id, expected=None, *, require_failure=True):
    account, binding, workspace, client = await context(db, account_id)
    stamp = {"remote_id": binding.remote_account_id, "target_url": binding.target_url,
             "workspace_id": workspace.id, "official_workspace_id": binding.official_workspace_id,
             "user_id": account.official_user_id, "email": account.email,
             "revision": int(account.credential_revision or 1)}
    if expected is not None and stamp != expected:
        raise RecoveryStopped("binding_changed", "账号身份、绑定或凭据已变化，未覆盖新的凭据")
    await db.commit()
    remote = await client.detail(stamp["remote_id"])
    if not codex_status.identity_matches(remote, account, binding):
        raise RecoveryStopped("identity_mismatch", "rs 邮箱、用户或团队与本地绑定不一致")
    if remote.get("enabled") is not True:
        raise RecoveryStopped("remote_disabled", "rs 账号已停用，不自动重新启用")
    if require_failure and not codex_status.auth_failed(remote):
        raise RecoveryStopped("not_auth_failed", "rs 当前不是明确的授权失效，不启动邮箱登录")
    return account, binding, workspace, client, stamp


async def last_operation(db, account_id):
    return await db.scalar(select(Operation).where(Operation.account_id == account_id,
        Operation.op_type == "codex_reauth").order_by(Operation.id.desc()).limit(1))


async def eligible_auto(db, account, remote_health):
    if not remote_health.get("can_reauth") or not await codex_status.reauth_enabled(db):
        return False
    if account.next_reauth_at and as_utc(account.next_reauth_at) > utcnow():
        return False
    previous = await last_operation(db, account.id)
    if previous and previous.state in {*ACTIVE_STATES, "manual_required", "partial", "cancelled"}:
        return False
    return int(account.reauth_fail_count or 0) < MAX_ATTEMPTS


async def run_once(db):
    if not await codex_status.reauth_enabled(db) or not command_runner.ready:
        return
    snapshot = await codex_status.load_snapshot(db)
    ids = list(await db.scalars(select(CodexBinding.account_id).order_by(CodexBinding.account_id)))
    for account_id in ids:
        account = await db.get(Account, account_id, populate_existing=True)
        binding = await db.get(CodexBinding, account_id, populate_existing=True)
        if account and binding and await eligible_auto(db, account, codex_status.present(snapshot, binding)):
            result = await start(db, account_id, automatic=True)
            if result.get("ok") or result.get("error_code") == "browser_busy":
                break


async def start(db, account_id, *, automatic=False):
    async with _LOCK:
        if not command_runner.ready:
            return fail("runner_unavailable", "后台执行器未启动")
        try:
            account, binding, workspace, client, stamp = await verified_remote(db, account_id)
            execution = await reauth_service.execution_context(db, account)
            if not execution["proxy"]["url"] or not execution["mailbox"]["effective_ready"]:
                return fail("mail_proxy_missing", "缺少代理或可读取的验证码邮箱，请先配置")
            hme = await load_hme_config(db)
            if execution["mailbox"]["route"] == "hme" and not hme.configured:
                return fail("mail_missing", "HME 连接未配置")
            if automatic and not await codex_status.reauth_enabled(db):
                return fail("automation_disabled", "自动重授权已关闭")
            from app.application.manual_rotation import unresolved_for_workspace
            if await unresolved_for_workspace(db, workspace.id):
                return fail("rotation_unresolved", "团队有未完成轮转，请先处理")
            # The first write serializes duplicate starts and manual OAuth creation.
            await db.execute(update(Account).where(Account.id == account_id).values(version=Account.version))
            account = await db.get(Account, account_id, populate_existing=True)
            if account.credential_revision != stamp["revision"]:
                raise RecoveryStopped("binding_changed", "凭据已变化，等待重新核对")
            from app.persistence.models.quota import CredentialLease
            lease = await db.get(CredentialLease, account_id)
            if lease and lease.token:
                return fail("refresh_in_flight", "正在读回凭据，等待完成后再重授权")
            if await operation_store.active_for_email(db, account.email):
                return fail("already_running", "该账号已有进行中任务")
            if await operation_store.browser_busy(db):
                return fail("browser_busy", "浏览器正被占用，等待下一轮")
            if await db.scalar(select(OAuthSession.id).where(OAuthSession.account_id == account_id,
                    OAuthSession.status.in_(("waiting", "exchanging")), OAuthSession.expires_at > utcnow()).limit(1)):
                return fail("oauth_busy", "该账号已有未完成的授权，请先完成或等待过期")
            previous = await last_operation(db, account_id)
            # Once OAuth succeeded, retry only the saved push, never request another login.
            publish_only = bool(previous and previous.state in {"partial", "manual_required", "failed", "cancelled"}
                                and await operation_store.step_succeeded(db, previous, "authorized")
                                and account.refresh_token_encrypted)
            row, blocker = await operation_store.create_workspace_locked(db, op_type="codex_reauth",
                workspace_id=workspace.id, account_id=account_id, email=account.email,
                input_payload={**stamp, "automatic": automatic, "publish_only": publish_only},
                source="automatic" if automatic else "manual")
            if blocker:
                return fail("operation_conflict", "团队有进行中的任务")
            if not automatic:
                account.reauth_fail_count = 0
            public_id = row.public_id
            await db.commit()
            command_runner.spawn(public_id, lambda session: _run(session, public_id))
            return {"ok": True, "accepted": True, "operation_id": public_id, "status": "running"}
        except RecoveryStopped as exc:
            await db.rollback()
            return fail(exc.code, str(exc))
        except Exception:
            await db.rollback()
            return fail("remote_unavailable", "无法确认 rs 状态，未启动自动登录")
        finally:
            await db.commit()


class FrozenProxyClient(ChatGPTClient):
    """OAuth exchange uses the same frozen proxy as the browser, including owner fallback."""
    def __init__(self, proxy):
        super().__init__()
        self.proxy = proxy

    async def _proxy_for(self, db_session, identifier):
        return self.proxy


async def _checkpoint(db, row, expected, *, require_failure=True):
    await db.refresh(row)
    if row.cancel_requested:
        raise RecoveryStopped("cancelled", "已停止；不重发授权、不改官方团队")
    if unpack_input(row.input_json).get("automatic") and not await codex_status.reauth_enabled(db):
        raise RecoveryStopped("automation_disabled", "自动重授权已关闭，停止后续步骤")
    return await verified_remote(db, row.account_id, expected, require_failure=require_failure)


async def _run(db, public_id):
    row = await operation_store.get_by_public_id(db, public_id)
    expected = {key: value for key, value in unpack_input(row.input_json).items()
                if key not in {"automatic", "publish_only"}}
    browser = InvitedBrowserSession()
    stored = None
    ticket = None
    exchanger = None
    account_id = row.account_id
    try:
        if not await browser.try_reserve():
            return fail("browser_busy", "浏览器正被占用，请稍后重试", manual=False)
        account, binding, workspace, client, _ = await _checkpoint(db, row, expected)
        publish_only = unpack_input(row.input_json).get("publish_only")
        if not publish_only:
            execution = await reauth_service.execution_context(db, account)
            proxy = execution["proxy"]["url"]
            route = execution["mailbox"]["route"]
            if not proxy or not execution["mailbox"]["effective_ready"]:
                raise RecoveryStopped("mail_proxy_missing", "缺少代理或可读取的邮箱")
            cf, hme = await load_cf_config(db), await load_hme_config(db)
            auth = chatgpt_client.create_oauth_authorize_url(oauth_sessions.CLIENT_ID, oauth_sessions.REDIRECT_URI,
                                                          login_hint=account.email)
            public = oauth_sessions.create_session(team_id=0, email=account.email, authorize=auth,
                                                   mode="auto", proxy=proxy)
            ticket = public["ticket"]
            oauth_sessions.mark_session(ticket, job_id=public_id)
            # Dedicated purpose: guarded by the original rs binding, not the generic local-refresh path.
            stored = await oauth_session_store.persist(db, oauth_sessions.get_session(ticket), purpose="codex_reauth",
                account_id=account_id, workspace_id=workspace.id, credential_revision=expected["revision"])
            await operation_store.note(db, row, "authorizing", "通过代理打开原账号，使用邮箱验证码重新授权")
            await db.commit()

            async def progress(stage, message):
                await db.refresh(row)
                if row.cancel_requested:
                    raise RecoveryStopped("cancelled", "已取消邮箱重授权")
                await operation_store.note(db, row, stage, message or "浏览器运行中")
                await db.commit()

            async with asyncio.timeout(900):
                result = await browser.run(email=account.email, password="", authorize_url=auth["authorize_url"],
                    proxy=proxy, pickup_url=execution["pickup_url"], login_only=True, allow_signup=False, allow_join=False,
                    allow_sms=False, team_name=workspace.official_name or workspace.name or "", use_cloudflare=route == "cloudflare",
                    cf_base_url=cf["base_url"], cf_address=cf["address"], cf_admin_password=cf["admin_password"],
                    hme_base_url=hme.base_url if route == "hme" else "",
                    hme_service_token=hme.service_token if route == "hme" else "",
                    hme_account_id=(account.hme_account_id or "") if route == "hme" else "", on_stage=progress)
            if not result.get("ok"):
                code = str(result.get("error_code") or "browser_failed")
                raise RecoveryStopped(code, "邮箱重授权未完成；保留原账号，请查看任务阶段或人工授权")
            account, binding, workspace, client, _ = await _checkpoint(db, row, expected)
            stored, parsed = await oauth_session_store.begin_exchange(db, ticket, result.get("callback_url") or "",
                                                                      account_id=account_id, purpose="codex_reauth")
            exchanger = FrozenProxyClient(proxy)
            exchanged = await exchanger.exchange_oauth_code(parsed["code"], oauth_sessions.CLIENT_ID,
                oauth_sessions.REDIRECT_URI, oauth_session_store.exchange_context(stored)["code_verifier"], db,
                identifier=account.email)
            from app.application.codex_publish import _token_identity
            identity = _token_identity(exchanged.get("access_token") or "") or {}
            if (not exchanged.get("success") or not exchanged.get("refresh_token")
                    or identity.get("email") != expected["email"].lower()
                    or identity.get("account_id") != expected["official_workspace_id"]
                    or identity.get("user_id") != expected["user_id"]):
                raise RecoveryStopped("identity_mismatch", "授权结果缺失或邮箱、用户、Team 不匹配，没有写入")
            await _checkpoint(db, row, expected)
            await db.execute(update(Account).where(Account.id == account_id).values(version=Account.version))
            account = await db.get(Account, account_id, populate_existing=True)
            if account.credential_revision != expected["revision"]:
                raise RecoveryStopped("binding_changed", "授权期间凭据已更新，没有覆盖")
            await auth_service.apply_tokens(account, {**exchanged, "client_id": oauth_sessions.CLIENT_ID})
            await oauth_session_store.finish(db, stored, success=True)
            await operation_store.mark_step(db, row, "authorized", state="success")
            await db.commit()
            expected["revision"] = account.credential_revision
        else:
            await operation_store.mark_step(db, row, "authorized", state="success")
            await db.commit()
        await _checkpoint(db, row, expected)
        await operation_store.note(db, row, "codex_push", "更新原 rs 账号，保留分组、代理和调度设置")
        await db.commit()
        from app.application.codex_publish import import_account
        pushed = await import_account(db, account_id, reenable=False, expected_remote_id=expected["remote_id"])
        if not pushed.get("ok") or pushed.get("remote_account_id") != expected["remote_id"]:
            return fail("codex_push_pending", "已授权，但 rs 更新未确认；再次恢复只重试推送，不重复登录")
        remote = await client.detail(expected["remote_id"])
        if (not codex_status.identity_matches(remote, account, binding) or remote.get("enabled") is not True
                or remote.get("status") not in {"normal", "rate_limited", "quota_exhausted"}):
            return fail("codex_verify_failed", "已更新凭据，但 rs 仍异常，请人工核对")
        account = await db.get(Account, account_id, populate_existing=True)
        account.reauth_fail_count = 0
        account.last_reauth_code = None
        account.next_reauth_at = utcnow() + timedelta(minutes=30)
        await operation_store.mark_step(db, row, "codex_push", state="success")
        await db.commit()
        await codex_status.refresh(db, force=True)
        return {"ok": True, "success": True, "status": "success", "message": "邮箱重授权完成，原 rs 账号已更新并核对"}
    except (RecoveryStopped, OAuthSessionError, TimeoutError) as exc:
        await db.rollback()
        account = await db.get(Account, account_id)
        code = getattr(exc, "code", getattr(exc, "error_code", "browser_timeout"))
        if account:
            account.reauth_fail_count = int(account.reauth_fail_count or 0) + 1
            account.last_reauth_code = str(code)[:40]
            account.next_reauth_at = utcnow() + timedelta(minutes=15 * min(account.reauth_fail_count, 4))
            await db.commit()
        result = fail(code, str(exc) if not isinstance(exc, TimeoutError) else "浏览器超时，已关闭窗口",
                      manual=code in MANUAL_CODES or code not in {"browser_failed", "mail_otp_timeout", "browser_timeout"}
                      or account is None or account.reauth_fail_count >= MAX_ATTEMPTS)
        if code == "cancelled":
            result["status"] = "cancelled"
        return result
    except Exception:
        await db.rollback()
        return fail("codex_reauth_failed", "rs 重授权异常中断，请核对原账号和任务记录后重试")
    finally:
        await browser.close()
        if exchanger:
            await exchanger.clear_session(expected["email"])
        if ticket:
            oauth_sessions.pop_session(ticket)
        if stored is not None:
            await db.refresh(stored)
            if stored.status in {"waiting", "exchanging"}:
                await oauth_session_store.finish(db, stored, success=False)
                await db.commit()
        # Rollback expires ORM objects; the command runner still owns this row.
        await db.refresh(row)
