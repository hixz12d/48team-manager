import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlencode, urlparse

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.operations import operation_store
from app.application.presenters import build_auth_status
from app.application.queries.identity import workspaces_query
from app.application.reauth import reauth_service
from app.application.tokens import auth_service, encrypt_secret
from app.core.crypto import token_cipher
from app.domain.reauth import (
    auto_reauth_plan,
    http_401_is_not_ban,
    is_icloud_email,
    is_oauth_callback,
    looks_like_deactivated,
    owner_refresh_allows_oauth,
    reauth_backoff_at,
    reauth_terminal_status,
)
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from app.persistence.models.settings import SystemSetting


WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"


def callback_url(started: dict, code: str) -> str:
    state = parse_qs(urlparse(started["authorize_url"]).query)["state"][0]
    return f"http://localhost:1455/auth/callback?{urlencode({'code': code, 'state': state})}"


class ReauthPolicyTests(unittest.TestCase):
    def test_owner_is_manual(self):
        plan = auto_reauth_plan(email="mom@gmail.com", role="owner", password="x", proxy="socks5h://u:p@1.2.3.4:1080")
        self.assertFalse(plan["auto"])

    def test_deactivated_copy_stops_reauth(self):
        self.assertTrue(looks_like_deactivated(body="This account has been deactivated."))
        self.assertTrue(looks_like_deactivated(error="account_deactivated"))
        self.assertFalse(looks_like_deactivated(body="Enter your password"))

    def test_deactivated_marks_manual_required(self):
        self.assertEqual(reauth_terminal_status(success=False, error_code="account_deactivated"), "manual_required")
        self.assertEqual(reauth_terminal_status(success=False, error_code="identity_conflict"), "manual_required")
        self.assertEqual(reauth_terminal_status(success=False, error_code="browser_failed"), "failed")
        self.assertEqual(reauth_terminal_status(success=True), "success")

    def test_401_is_not_ban(self):
        self.assertTrue(http_401_is_not_ban("http_401", 401))
        self.assertTrue(http_401_is_not_ban("token_invalidated"))
        self.assertFalse(http_401_is_not_ban("account_deactivated", 403))

    def test_owner_refresh_falls_back_except_identity_mismatch(self):
        self.assertTrue(owner_refresh_allows_oauth("token_refresh_failed"))
        self.assertFalse(owner_refresh_allows_oauth("token_identity_mismatch"))

    def test_icloud_with_password_mail_and_proxy_is_auto(self):
        self.assertTrue(is_icloud_email("kid@icloud.com"))
        plan = auto_reauth_plan(
            email="kid@icloud.com",
            role="child",
            password="secret",
            cf_ready=True,
            proxy="socks5h://u:p@1.2.3.4:1080",
        )
        self.assertTrue(plan["auto"])

    def test_icloud_without_proxy_falls_back(self):
        plan = auto_reauth_plan(email="kid@icloud.com", role="child", password="secret", cf_ready=True, proxy="")
        self.assertFalse(plan["auto"])

    def test_callback_detection(self):
        self.assertTrue(is_oauth_callback("http://localhost:1455/auth/callback?code=abc&state=1"))
        self.assertFalse(is_oauth_callback("https://auth.openai.com/oauth/authorize"))

    def test_backoff_grows_then_caps(self):
        now = datetime(2026, 3, 29, 12, 0, 0)
        first = reauth_backoff_at(now, 1)
        second = reauth_backoff_at(now, 2)
        later = reauth_backoff_at(now, 9)
        self.assertEqual(first, now + timedelta(hours=2))
        self.assertEqual(second, now + timedelta(hours=4))
        self.assertEqual(later, now + timedelta(hours=6))


    def test_auth_presenter_has_one_canonical_mapping(self):
        missing = Account(
            email="new@example.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="oauth_required",
        )
        self.assertEqual(
            build_auth_status(missing),
            {
                "auth_state": "oauth_required",
                "needs_auth": True,
                "auth_action": "authorize",
                "auth_reason": "missing_token",
            },
        )
        missing.access_token_encrypted = "encrypted"
        self.assertEqual(build_auth_status(missing)["auth_action"], "reauthorize")
        missing.auth_state = "healthy"
        self.assertFalse(build_auth_status(missing)["needs_auth"])
        missing.auth_state = "deactivated"
        deactivated = build_auth_status(missing)
        self.assertTrue(deactivated["needs_auth"])
        self.assertIsNone(deactivated["auth_action"])
        self.assertEqual(deactivated["auth_reason"], "deactivated")


class AuthProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_refresh_success_does_not_open_browser(self):
        account = Account(
            email="kid@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="refresh_due",
            refresh_token_encrypted=encrypt_secret("rt-old"),
            client_id="app_test",
        )
        self.session.add(account)
        await self.session.commit()

        class Client:
            async def refresh_access_token(self, refresh_token, client_id, db_session, identifier="default"):
                self.called = (refresh_token, client_id, identifier)
                return {"success": True, "access_token": "at-new", "refresh_token": "rt-new"}

        client = Client()
        service = auth_service.__class__(client=client)
        result = await service.refresh_account(self.session, account)
        self.assertTrue(result["success"])
        self.assertEqual(account.auth_state, "healthy")
        self.assertEqual(token_cipher().decrypt(account.access_token_encrypted), "at-new")

    async def test_refresh_401_becomes_oauth_required_not_deactivated(self):
        account = Account(
            email="kid@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="unknown",
            refresh_token_encrypted=encrypt_secret("rt-old"),
        )
        self.session.add(account)
        await self.session.commit()

        class Client:
            async def refresh_access_token(self, refresh_token, client_id, db_session, identifier="default"):
                return {"success": False, "status_code": 401, "error_code": "http_401", "error": "expired"}

        result = await auth_service.__class__(client=Client()).refresh_account(self.session, account)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "token_refresh_failed")
        self.assertTrue(result["allow_oauth"])
        self.assertEqual(account.auth_state, "oauth_required")
        self.assertNotEqual(account.auth_state, "deactivated")


class ReauthGateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_disabled_by_default_does_not_queue(self):
        account = Account(
            email="kid@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="oauth_required",
            proxy="socks5h://127.0.0.1:1080",
            password_encrypted=encrypt_secret("secret"),
            mail_raw="kid@icloud.com----https://mail.example/pickup",
        )
        self.session.add(account)
        await self.session.commit()
        stats = await reauth_service.run_once(self.session, settings={"enabled": False, "interval_minutes": 30})
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["queued"], 0)

    async def test_conflict_and_owner_are_manual_required(self):
        child = Account(
            email="kid@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="oauth_required",
            proxy="socks5h://127.0.0.1:1080",
            password_encrypted=encrypt_secret("secret"),
            mail_raw="kid@icloud.com----https://mail.example/pickup",
        )
        owner = Account(
            email="owner@icloud.com",
            official_plan="unknown",
            local_purpose="mother",
            operational_state="active",
            auth_state="oauth_required",
            proxy="socks5h://127.0.0.1:1080",
            password_encrypted=encrypt_secret("secret"),
        )
        self.session.add_all([child, owner])
        await self.session.flush()
        self.session.add(
            ExternalBinding(
                provider="sub2api",
                local_account_id=child.id,
                remote_account_id="77",
                binding_state="conflict",
                last_error="email mismatch",
            )
        )
        workspace = Workspace(official_workspace_id=WORKSPACE_UUID, owner_account_id=owner.id, status="active")
        self.session.add(workspace)
        await self.session.flush()
        self.session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                account_id=owner.id,
                official_role="owner",
                membership_state="joined",
                local_purpose="mother",
            )
        )
        await self.session.commit()
        blocked = await reauth_service.start_auto_reauth(self.session, child)
        self.assertFalse(blocked["success"])
        self.assertEqual(blocked["error_code"], "identity_conflict")
        self.assertEqual(blocked["status"], "manual_required")
        owner_blocked = await reauth_service.start_auto_reauth(self.session, owner)
        self.assertEqual(owner_blocked["error_code"], "owner_manual")
        refreshed = await self.session.get(Account, child.id)
        self.assertEqual(refreshed.auth_state, "manual_required")

    async def test_execution_context_inherits_owner_proxy_and_uses_cloudflare_mail(self):
        owner = Account(
            email="owner@gmail.com",
            official_plan="team",
            local_purpose="mother",
            operational_state="active",
            auth_state="healthy",
            proxy="socks5h://owner-proxy.example:1080",
            proxy_source="legacy",
        )
        child = Account(
            email="child@icloud.com",
            official_plan="team",
            local_purpose="child",
            operational_state="active",
            auth_state="oauth_required",
            mailbox_read_state="unknown",
        )
        self.session.add_all([owner, child])
        await self.session.flush()
        workspace = Workspace(
            official_workspace_id=WORKSPACE_UUID,
            owner_account_id=owner.id,
            status="active",
        )
        self.session.add(workspace)
        await self.session.flush()
        self.session.add_all([
            WorkspaceMembership(
                workspace_id=workspace.id,
                account_id=child.id,
                official_role="member",
                membership_state="invited",
                local_purpose="child",
            ),
            SystemSetting(key="cf_mail_admin_password", value="configured-secret"),
        ])
        await self.session.commit()

        context = await reauth_service.execution_context(self.session, child)

        self.assertEqual(context["proxy"]["url"], owner.proxy)
        self.assertEqual(context["proxy"]["origin"], "workspace_owner")
        self.assertEqual(context["proxy"]["account_id"], owner.id)
        self.assertTrue(context["mailbox"]["effective_ready"])
        self.assertEqual(context["mailbox"]["route"], "cloudflare")

        started = await reauth_service.start_auto_reauth(self.session, child)
        self.assertTrue(started["success"])
        operation = await operation_store.get_by_public_id(self.session, started["job_id"])
        self.assertEqual(operation.resolved_proxy, owner.proxy)

    async def test_browser_busy_does_not_start_second_job(self):
        first = Account(
            email="one@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="oauth_required",
        )
        second = Account(
            email="two@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="oauth_required",
            proxy="socks5h://127.0.0.1:1080",
            password_encrypted=encrypt_secret("secret"),
            mail_raw="two@icloud.com----https://mail.example/pickup",
        )
        self.session.add_all([first, second])
        await self.session.commit()
        await operation_store.create(self.session, op_type="reauth", account_id=first.id, email=first.email)
        await self.session.commit()
        result = await reauth_service.start_auto_reauth(self.session, second)
        self.assertTrue(result["skipped"])
        self.assertEqual(result["error_code"], "browser_busy")

    async def test_restart_recovery_does_not_resume_playwright(self):
        account = Account(
            email="kid@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            auth_state="oauth_required",
        )
        self.session.add(account)
        await self.session.flush()
        row = await operation_store.create(self.session, op_type="reauth", account_id=account.id, email=account.email)
        await self.session.commit()
        from app.application.operations import recover_stale_operations

        stats = await recover_stale_operations(self.session)
        self.assertEqual(stats["manual"], 1)
        refreshed = await self.session.get(type(row), row.id)
        self.assertEqual(refreshed.state, "manual_required")


class ManualReauthLinkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _account(self, email: str, purpose: str) -> Account:
        account = Account(
            email=email,
            official_plan="unknown",
            local_purpose=purpose,
            operational_state="active",
            auth_state="oauth_required",
        )
        self.session.add(account)
        await self.session.commit()
        await self.session.refresh(account)
        return account

    async def test_mother_and_child_manual_reauth_return_authorize_url(self):
        mother = await self._account("mom@gmail.com", "mother")
        child = await self._account("kid@icloud.com", "child")
        for account in (mother, child):
            started = await reauth_service.start_manual_reauth(self.session, account)
            self.assertTrue(started["ok"])
            self.assertTrue(started["ticket"])
            self.assertIn("auth.openai.com/oauth/authorize", started["authorize_url"])
            self.assertEqual(started["email"], account.email)

    async def test_complete_writes_tokens_without_browser(self):
        account = await self._account("kid@icloud.com", "child")
        started = await reauth_service.start_manual_reauth(self.session, account)
        client = AsyncMock()
        client.exchange_oauth_code = AsyncMock(
            return_value={
                "success": True,
                "access_token": "at-new",
                "refresh_token": "rt-new",
                "id_token": "id-new",
            }
        )
        result = await reauth_service.complete_manual_reauth(
            self.session,
            account,
            ticket=started["ticket"],
            callback_url=callback_url(started, "abc"),
            client=client,
        )
        self.assertTrue(result.get("ok"), result)
        refreshed = await self.session.get(Account, account.id)
        self.assertEqual(refreshed.auth_state, "healthy")
        self.assertTrue(refreshed.access_token_encrypted)


    async def test_start_and_failed_complete_preserve_existing_auth(self):
        account = await self._account("stable@example.com", "mother")
        account.auth_state = "healthy"
        account.access_token_encrypted = encrypt_secret("at-existing")
        await self.session.commit()
        original_token = account.access_token_encrypted

        started = await reauth_service.start_manual_reauth(self.session, account)
        refreshed = await self.session.get(Account, account.id)
        self.assertEqual(refreshed.auth_state, "healthy")
        self.assertEqual(refreshed.access_token_encrypted, original_token)

        client = AsyncMock()
        client.exchange_oauth_code = AsyncMock(
            return_value={"success": False, "error": "revoked", "error_code": "token_revoked"}
        )
        failed = await reauth_service.complete_manual_reauth(
            self.session,
            account,
            ticket=started["ticket"],
            callback_url=callback_url(started, "bad"),
            client=client,
        )
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error_code"], "token_revoked")
        refreshed = await self.session.get(Account, account.id)
        self.assertEqual(refreshed.auth_state, "healthy")
        self.assertEqual(refreshed.access_token_encrypted, original_token)

    async def test_expired_callback_has_stable_error_code(self):
        account = await self._account("expired@example.com", "child")
        result = await reauth_service.complete_manual_reauth(
            self.session,
            account,
            ticket="missing-ticket",
            callback_url="http://localhost:1455/auth/callback?code=late",
        )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "callback_expired")
        self.assertEqual(account.auth_state, "oauth_required")

    async def test_owner_reauth_updates_workspace_auth_health(self):
        owner = await self._account("owner@example.com", "mother")
        workspace = Workspace(
            official_workspace_id=WORKSPACE_UUID,
            owner_account_id=owner.id,
            status="active",
        )
        self.session.add(workspace)
        await self.session.flush()
        self.session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                account_id=owner.id,
                official_role="owner",
                membership_state="joined",
                local_purpose="mother",
            )
        )
        await self.session.commit()
        before = (await workspaces_query(self.session))["items"][0]
        self.assertTrue(before["owner_needs_auth"])
        self.assertEqual(before["health"], "needs_auth")

        started = await reauth_service.start_manual_reauth(self.session, owner)
        client = AsyncMock()
        client.exchange_oauth_code = AsyncMock(
            return_value={"success": True, "access_token": "at-new", "refresh_token": "rt-new"}
        )
        completed = await reauth_service.complete_manual_reauth(
            self.session,
            owner,
            ticket=started["ticket"],
            callback_url=callback_url(started, "ok"),
            client=client,
        )
        self.assertTrue(completed["ok"])
        after = (await workspaces_query(self.session))["items"][0]
        self.assertEqual(after["owner_auth_state"], "healthy")
        self.assertFalse(after["owner_needs_auth"])
        self.assertEqual(after["health"], "not_synced")
