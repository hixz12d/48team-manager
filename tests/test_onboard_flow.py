import inspect
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Team
from app.services.child_accounts import child_account_service
from app.services.browser_onboard import (
    can_peek_session,
    looks_like_about_you,
    looks_like_cloudflare,
    looks_like_email_gate,
    looks_like_session_ended,
    looks_like_invalid_phone,
    split_phone,
    looks_like_otp_input,
    session_access_token,
)
from app.services.onboard import OnboardService, classify_onboard_error, should_wait_for_invite_mail
from app.utils.time_utils import get_now


class OnboardHelperTests(unittest.TestCase):
    def test_classify_coroutine_bug(self):
        self.assertEqual(
            classify_onboard_error("'coroutine' object has no attribute 'get'"),
            "browser_await_bug",
        )

    def test_run_browser_is_sync(self):
        self.assertFalse(inspect.iscoroutinefunction(OnboardService._run_browser))

    def test_session_helpers(self):
        self.assertTrue(can_peek_session("https://chatgpt.com/invite/abc"))
        self.assertFalse(can_peek_session("https://auth.openai.com/create-account"))
        self.assertEqual(
            session_access_token({"status": 200, "json": {"accessToken": "tok"}}),
            "tok",
        )
        self.assertEqual(session_access_token({"status": 200, "json": {}}), "")

    def test_cloudflare_detection(self):
        self.assertTrue(looks_like_cloudflare("Just a moment...", "Verifying..."))
        self.assertFalse(looks_like_cloudflare("Accept invite | ChatGPT", "Join workspace"))
        self.assertEqual(
            classify_onboard_error("cloudflare challenge; no accessToken"),
            "cloudflare_challenge",
        )
        self.assertEqual(
            classify_onboard_error("OpenAI 限流：验证码试太多次"),
            "openai_rate_limited",
        )
        self.assertEqual(
            classify_onboard_error("邮箱验证码提交后仍未通过，没有继续连交"),
            "mail_otp_rejected",
        )

    def test_email_gate_detection(self):
        self.assertTrue(looks_like_email_gate(
            title="Get started | ChatGPT",
            url="https://chatgpt.com/auth/login?email=a@b.com",
        ))
        self.assertTrue(looks_like_email_gate(
            title="Log in or sign up",
            body="Email address",
            url="https://chatgpt.com/auth/login",
        ))
        self.assertFalse(looks_like_email_gate(
            title="Check your inbox - OpenAI",
            url="https://auth.openai.com/email-verification",
        ))
        self.assertEqual(
            classify_onboard_error("卡在 ChatGPT 邮箱页，没有进入 OpenAI 注册"),
            "email_gate_stuck",
        )
        self.assertTrue(looks_like_session_ended(title="Your session has ended - OpenAI", body="Continue by logging in"))
        self.assertFalse(looks_like_session_ended(title="Get started | ChatGPT", body="Log in or sign up"))
        self.assertEqual(split_phone("+8613434986375"), ("China", "13434986375"))
        self.assertEqual(split_phone("8613434986"), ("China", "13434986"))
        self.assertTrue(looks_like_invalid_phone("Phone number is not valid."))
        self.assertFalse(looks_like_invalid_phone("Enter your phone number"))
        self.assertEqual(split_phone("+13434986375"), ("United States", "3434986375"))

    def test_age_page_is_not_otp(self):
        self.assertTrue(looks_like_about_you(title="How old are you? - OpenAI", body="How old are you?", url="https://auth.openai.com/about-you"))
        self.assertFalse(looks_like_otp_input(name="age", placeholder="Age", input_type="text"))
        self.assertTrue(looks_like_otp_input(name="code", placeholder="Code", autocomplete="one-time-code"))

    def test_skip_invite_mail_when_already_invited(self):
        self.assertFalse(should_wait_for_invite_mail(True))
        self.assertFalse(should_wait_for_invite_mail(False))

    def test_classify_oauth_errors(self):
        self.assertEqual(classify_onboard_error("授权成功但没有 refresh_token，未推送"), "oauth_no_refresh")
        self.assertEqual(classify_onboard_error("登录的是 a@b.com，不是 c@d.com"), "oauth_identity_mismatch")
        self.assertEqual(classify_onboard_error("无法自动授权", stage="oauth"), "oauth_failed")
        self.assertEqual(
            classify_onboard_error("手机号不被 OpenAI 接受。Codex 授权通常不吃 +86，要换能过的接码号"),
            "sms_rejected",
        )
        self.assertEqual(classify_onboard_error("需要接码，但未提供手机号"), "sms_failed")
        self.assertEqual(classify_onboard_error("卡在手机号页，没能发出短信"), "sms_failed")

    def test_run_oauth_browser_is_sync(self):
        self.assertFalse(inspect.iscoroutinefunction(OnboardService._run_oauth_browser))


class OnboardKickTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _team(self) -> Team:
        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            account_id="acc-1",
            max_members=5,
            current_members=2,
            proxy="socks5h://127.0.0.1:1080",
            status="active",
            account_role="account-owner",
        )
        self.session.add(team)
        await self.session.flush()
        return team

    async def test_revoke_invite_returns_unused_not_standby(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        await child_account_service.mark_invited(self.session, child, team)
        service = OnboardService()
        service._load_team = AsyncMock(return_value=team)
        service._team_proxy = MagicMock(return_value="socks5h://127.0.0.1:1080")
        service._lookup_live_member = AsyncMock(return_value=(
            {"success": True, "members": [{"email": "kid@example.com", "status": "invited"}]},
            {"email": "kid@example.com", "status": "invited", "user_id": None},
        ))
        from app.services import team as team_mod
        original = team_mod.team_service.revoke_team_invite
        team_mod.team_service.revoke_team_invite = AsyncMock(return_value={"success": True, "message": "ok"})
        try:
            result = await service.kick_to_standby(self.session, team_id=team.id, email="kid@example.com")
        finally:
            team_mod.team_service.revoke_team_invite = original
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "revoked")
        self.assertIn("未使用", result["message"])
        self.assertEqual(child.status, "unused")
        self.assertIsNone(child.kicked_at)

    async def test_kick_joined_goes_standby(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        await child_account_service.mark_active(self.session, child, team)
        service = OnboardService()
        service._load_team = AsyncMock(return_value=team)
        service._team_proxy = MagicMock(return_value="socks5h://127.0.0.1:1080")
        joined = {"email": "kid@example.com", "status": "joined", "user_id": "user-1", "account_user_id": "user-1"}
        service._lookup_live_member = AsyncMock(side_effect=[
            ({"success": True, "members": [joined]}, joined),
            ({"success": True, "members": []}, None),
        ])
        from app.services import team as team_mod
        original = team_mod.team_service.delete_team_member
        team_mod.team_service.delete_team_member = AsyncMock(return_value={"success": True, "message": "ok"})
        try:
            result = await service.kick_to_standby(self.session, team_id=team.id, email="kid@example.com")
        finally:
            team_mod.team_service.delete_team_member = original
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "standby")
        self.assertEqual(child.status, "standby")

    async def test_kick_fails_when_member_still_live(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        await child_account_service.mark_active(self.session, child, team)
        service = OnboardService()
        service._load_team = AsyncMock(return_value=team)
        service._team_proxy = MagicMock(return_value="socks5h://127.0.0.1:1080")
        joined = {"email": "kid@example.com", "status": "joined", "user_id": "user-1", "account_user_id": "user-alt"}
        service._lookup_live_member = AsyncMock(return_value=(
            {"success": True, "members": [joined]}, joined,
        ))
        from app.services import team as team_mod
        original = team_mod.team_service.delete_team_member
        delete_mock = AsyncMock(return_value={"success": True, "already_removed": True})
        team_mod.team_service.delete_team_member = delete_mock
        try:
            result = await service.kick_to_standby(self.session, team_id=team.id, email="kid@example.com")
        finally:
            team_mod.team_service.delete_team_member = original
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "kick_not_removed")
        self.assertEqual(child.status, "active")
        self.assertGreaterEqual(delete_mock.await_count, 2)

    async def test_master_degraded_blocks_invite(self):
        team = await self._team()
        team.account_role = "standard-user"
        service = OnboardService()
        service._load_team = AsyncMock(return_value=team)
        service._team_proxy = MagicMock(return_value="socks5h://127.0.0.1:1080")
        result = await service.invite_and_onboard(self.session, team_id=team.id, email_line="new@example.com")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "master_degraded")

    async def test_kick_cooldown_blocks_reinvite(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        await child_account_service.mark_active(self.session, child, team)
        await child_account_service.mark_standby(self.session, child)
        child.kicked_at = get_now() - timedelta(minutes=1)
        service = OnboardService()
        service._load_team = AsyncMock(return_value=team)
        service._team_proxy = MagicMock(return_value="socks5h://127.0.0.1:1080")
        result = await service.invite_and_onboard(self.session, team_id=team.id, email_line="kid@example.com")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "kick_cooldown")

    async def test_serialize_invited_failure_label(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        await child_account_service.mark_invited(self.session, child, team)
        child.last_error = "email OTP not found"
        view = child_account_service.serialize(child)
        self.assertEqual(view["status_label"], "已邀请，注册失败")
        self.assertTrue(view["can_reregister"])

    async def test_serialize_active_unfinished_label(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        await child_account_service.mark_active(self.session, child, team)
        child.last_error = "授权成功但没有 refresh_token，未推送"
        view = child_account_service.serialize(child)
        self.assertEqual(view["status_label"], "已入组，未完成")
        self.assertFalse(view["can_reregister"])
        self.assertTrue(view["can_continue_auth"])

    async def test_serialize_free_unfinished_label(self):
        child = await child_account_service.upsert_from_input(self.session, email="free@icloud.com")
        await child_account_service.mark_free(self.session, child)
        child.last_error = "授权成功但没有 refresh_token，未推送"
        view = child_account_service.serialize(child)
        self.assertEqual(view["status_label"], "免费号，未完成")
        self.assertTrue(view["can_reregister"])


    async def test_live_lookup_failure_does_not_revoke(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        await child_account_service.mark_invited(self.session, child, team)
        service = OnboardService()
        service._load_team = AsyncMock(return_value=team)
        service._team_proxy = MagicMock(return_value="socks5h://127.0.0.1:1080")
        service._lookup_live_member = AsyncMock(return_value=(
            {"success": False, "error": "timeout", "members": []},
            None,
        ))
        result = await service.kick_to_standby(self.session, team_id=team.id, email="kid@example.com")
        self.assertFalse(result["success"])
        self.assertIn("未执行踢人", result["error"])
        self.assertEqual(child.status, "invited")

    async def test_apply_reconcile_fixes_leftover_local_only(self):
        team = await self._team()
        leftover = await child_account_service.upsert_from_input(self.session, email="left@example.com")
        await child_account_service.mark_invited(self.session, leftover, team)
        service = OnboardService()
        service._load_team = AsyncMock(return_value=team)
        from app.services import team as team_mod
        original = team_mod.team_service.get_team_members
        team_mod.team_service.get_team_members = AsyncMock(return_value={
            "success": True,
            "members": [{"email": "ghost@example.com", "status": "joined", "user_id": "user-9"}],
        })
        try:
            result = await service.apply_reconcile(self.session, team.id)
        finally:
            team_mod.team_service.get_team_members = original
        self.assertTrue(result["success"])
        self.assertEqual(leftover.status, "unused")
        self.assertTrue(any(item.get("type") == "ghost" for item in result["skipped"]))
        self.assertFalse(any(item.get("type") == "ghost" for item in result["applied"]))

    async def test_apply_reconcile_marks_invited_as_active_when_already_joined(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(self.session, email="basket@example.com")
        await child_account_service.mark_invited(self.session, child, team)
        service = OnboardService()
        service._load_team = AsyncMock(return_value=team)
        from app.services import team as team_mod
        original = team_mod.team_service.get_team_members
        team_mod.team_service.get_team_members = AsyncMock(return_value={
            "success": True,
            "members": [{"email": "basket@example.com", "status": "joined", "added_at": "2026-08-20T01:00:00"}],
        })
        try:
            result = await service.apply_reconcile(self.session, team.id)
        finally:
            team_mod.team_service.get_team_members = original
        self.assertTrue(result["success"])
        self.assertEqual(child.status, "active")
        self.assertTrue(any(item.get("type") == "joined_unmarked" for item in result["applied"]))

    async def test_fix_child_account_id_uses_team_workspace(self):
        team = await self._team()
        team.account_id = "11111111-1111-4111-8111-111111111111"
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        await child_account_service.mark_active(self.session, child, team)
        child.account_id = "user-not-a-workspace"
        service = OnboardService()
        result = await service.fix_child_account_id(self.session, child_id=child.id, push=False)
        self.assertTrue(result["success"])
        self.assertEqual(result["account_id"], team.account_id)
        self.assertEqual(child.account_id, team.account_id)
        self.assertFalse(result["pushed"])
        self.assertFalse(child_account_service.serialize(child)["needs_account_id_fix"])


class OnboardOauthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _team(self) -> Team:
        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            account_id="acc-1",
            max_members=5,
            current_members=2,
            proxy="socks5h://127.0.0.1:1080",
            status="active",
            account_role="account-owner",
        )
        self.session.add(team)
        await self.session.flush()
        return team

    def _patch_services(self):
        from app.services import onboard as onboard_mod

        originals = {
            "add_team_member": onboard_mod.team_service.add_team_member,
            "create_auth": onboard_mod.chatgpt_service.create_oauth_authorize_url,
            "exchange": onboard_mod.chatgpt_service.exchange_oauth_code,
            "import_session": onboard_mod.sub2api_service.import_session,
        }
        onboard_mod.team_service.add_team_member = AsyncMock(return_value={"success": True, "message": "invited"})
        onboard_mod.chatgpt_service.create_oauth_authorize_url = MagicMock(return_value={
            "authorize_url": "https://auth.openai.com/oauth/authorize",
            "code_verifier": "ver",
            "state": "st",
        })
        onboard_mod.chatgpt_service.exchange_oauth_code = AsyncMock(return_value={
            "success": True,
            "access_token": "at-oauth",
            "refresh_token": "rt-oauth",
            "id_token": "id-oauth",
        })
        onboard_mod.sub2api_service.import_session = AsyncMock(return_value={
            "account_id": 99,
            "strategy": "apply_oauth_credentials",
            "probe": {"kind": "200", "label": "200"},
        })
        mocks = {
            "add_team_member": onboard_mod.team_service.add_team_member,
            "import_session": onboard_mod.sub2api_service.import_session,
        }
        return onboard_mod, originals, mocks

    def _restore(self, onboard_mod, originals):
        onboard_mod.team_service.add_team_member = originals["add_team_member"]
        onboard_mod.chatgpt_service.create_oauth_authorize_url = originals["create_auth"]
        onboard_mod.chatgpt_service.exchange_oauth_code = originals["exchange"]
        onboard_mod.sub2api_service.import_session = originals["import_session"]

    def _service(self, team: Team) -> OnboardService:
        service = OnboardService()
        service._load_team = AsyncMock(return_value=team)
        service._team_proxy = MagicMock(return_value="socks5h://127.0.0.1:1080")
        service._child_proxy = MagicMock(return_value="socks5h://127.0.0.1:1080")
        service._cf_config = AsyncMock(return_value={
            "base_url": "https://cf.example",
            "address": "inbox@example.com",
            "admin_password": "pw",
        })
        service._confirm_joined = AsyncMock(return_value=True)
        service._lookup_live_member = AsyncMock(return_value=(
            {"success": True, "members": []},
            None,
        ))
        service._run_browser = MagicMock(return_value={
            "ok": True,
            "access_token": "at-browser",
            "refresh_token": "",
            "session_token": "st",
            "id_token": "",
            "account_id": "",
            "client_id": "",
            "password": "Passw0rd!",
        })
        service._run_oauth_browser = MagicMock(return_value={
            "ok": True,
            "callback_url": "http://localhost:1455/auth/callback?code=abc&state=st",
        })
        return service

    async def test_active_with_refresh_skips_everything(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(
            self.session, email="kid@example.com", password="Passw0rd!"
        )
        await child_account_service.mark_active(self.session, child, team)
        await child_account_service.save_tokens(self.session, child, {"refresh_token": "rt-existing"})
        service = self._service(team)
        onboard_mod, originals, mocks = self._patch_services()
        try:
            result = await service.invite_and_onboard(self.session, team_id=team.id, email_line="kid@example.com")
        finally:
            self._restore(onboard_mod, originals)
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "already_exists")
        service._run_browser.assert_not_called()
        service._run_oauth_browser.assert_not_called()
        mocks["import_session"].assert_not_called()

    async def test_already_joined_without_refresh_goes_oauth(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(
            self.session, email="kid@example.com", password="Passw0rd!"
        )
        child.mail_raw = "kid@example.com"
        service = self._service(team)
        joined = {"email": "kid@example.com", "status": "joined", "user_id": "user-1"}
        service._lookup_live_member = AsyncMock(return_value=(
            {"success": True, "members": [joined]}, joined,
        ))
        onboard_mod, originals, mocks = self._patch_services()
        try:
            result = await service.invite_and_onboard(self.session, team_id=team.id, email_line="kid@example.com")
        finally:
            self._restore(onboard_mod, originals)
        self.assertTrue(result["success"])
        self.assertTrue(result["oauth"])
        self.assertEqual(result["status"], "active")
        service._run_browser.assert_not_called()
        service._run_oauth_browser.assert_called_once()
        mocks["add_team_member"].assert_not_called()
        mocks["import_session"].assert_awaited_once()
        self.assertTrue(child_account_service.decrypt_secret(child.refresh_token_encrypted))

    async def test_register_then_oauth_then_push(self):
        team = await self._team()
        service = self._service(team)
        onboard_mod, originals, mocks = self._patch_services()
        try:
            result = await service.invite_and_onboard(
                self.session,
                team_id=team.id,
                email_line="new@icloud.com",
                phone_line="+15551234567----https://sms.example/key",
            )
        finally:
            self._restore(onboard_mod, originals)
        self.assertTrue(result["success"])
        self.assertTrue(result["oauth"])
        self.assertEqual(result["status"], "active")
        service._run_browser.assert_called_once()
        service._run_oauth_browser.assert_called_once()
        mocks["import_session"].assert_awaited_once()
        child = await child_account_service.get_by_email(self.session, "new@icloud.com")
        self.assertEqual(child_account_service.decrypt_secret(child.refresh_token_encrypted), "rt-oauth")
        self.assertEqual(child.sub2api_account_id, 99)

    async def test_oauth_failure_does_not_mark_done_or_push(self):
        team = await self._team()
        service = self._service(team)
        service._run_oauth_browser = MagicMock(return_value={"ok": False, "error": "email OTP timeout", "error_code": "mail_otp_timeout"})
        onboard_mod, originals, mocks = self._patch_services()
        try:
            result = await service.invite_and_onboard(self.session, team_id=team.id, email_line="new@icloud.com")
        finally:
            self._restore(onboard_mod, originals)
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "oauth_failed")
        self.assertEqual(result["error_code"], "mail_otp_timeout")
        mocks["import_session"].assert_not_called()
        child = await child_account_service.get_by_email(self.session, "new@icloud.com")
        self.assertEqual(child.status, "active")
        self.assertEqual(child.last_stage, "oauth_failed")
        self.assertFalse(child_account_service.decrypt_secret(child.refresh_token_encrypted))

    async def test_existing_refresh_skips_oauth_and_pushes(self):
        team = await self._team()
        service = self._service(team)
        service._run_browser = MagicMock(return_value={
            "ok": True,
            "access_token": "at-browser",
            "refresh_token": "rt-browser",
            "session_token": "st",
            "id_token": "",
            "password": "Passw0rd!",
        })
        onboard_mod, originals, mocks = self._patch_services()
        try:
            result = await service.invite_and_onboard(self.session, team_id=team.id, email_line="new@icloud.com")
        finally:
            self._restore(onboard_mod, originals)
        self.assertTrue(result["success"])
        self.assertFalse(result["oauth"])
        service._run_oauth_browser.assert_not_called()
        mocks["import_session"].assert_awaited_once()

    async def test_already_joined_without_password_does_not_invent_one(self):
        team = await self._team()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        service = self._service(team)
        joined = {"email": "kid@example.com", "status": "joined", "user_id": "user-1"}
        service._lookup_live_member = AsyncMock(return_value=(
            {"success": True, "members": [joined]}, joined,
        ))
        onboard_mod, originals, mocks = self._patch_services()
        try:
            result = await service.invite_and_onboard(self.session, team_id=team.id, email_line="kid@example.com")
        finally:
            self._restore(onboard_mod, originals)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "oauth_missing_password")
        service._run_oauth_browser.assert_not_called()
        mocks["import_session"].assert_not_called()
        self.assertFalse(child_account_service.decrypt_secret(child.password_encrypted))


class OnboardFreeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    def _patch(self):
        from app.services import onboard as onboard_mod
        from app.services.settings import settings_service

        originals = {
            "import_session": onboard_mod.sub2api_service.import_session,
            "create_auth": onboard_mod.chatgpt_service.create_oauth_authorize_url,
            "exchange": onboard_mod.chatgpt_service.exchange_oauth_code,
            "get_setting": settings_service.get_setting,
        }
        onboard_mod.sub2api_service.import_session = AsyncMock(return_value={
            "account_id": 77,
            "strategy": "apply_oauth_credentials",
            "probe": {"kind": "200", "label": "200"},
        })
        onboard_mod.chatgpt_service.create_oauth_authorize_url = MagicMock(return_value={
            "authorize_url": "https://auth.openai.com/oauth/authorize",
            "code_verifier": "ver",
            "state": "st",
        })
        onboard_mod.chatgpt_service.exchange_oauth_code = AsyncMock(return_value={
            "success": True,
            "access_token": "at-oauth",
            "refresh_token": "rt-oauth",
            "id_token": "id-oauth",
        })

        async def fake_setting(_db, key, default=""):
            if key == "free_account_proxy":
                return ""
            return default

        settings_service.get_setting = fake_setting
        return onboard_mod, settings_service, originals

    def _restore(self, onboard_mod, settings_service, originals):
        onboard_mod.sub2api_service.import_session = originals["import_session"]
        onboard_mod.chatgpt_service.create_oauth_authorize_url = originals["create_auth"]
        onboard_mod.chatgpt_service.exchange_oauth_code = originals["exchange"]
        settings_service.get_setting = originals["get_setting"]

    def _service(self) -> OnboardService:
        service = OnboardService()
        service._cf_config = AsyncMock(return_value={
            "base_url": "https://cf.example",
            "address": "inbox@example.com",
            "admin_password": "pw",
        })
        service._run_browser = MagicMock(return_value={
            "ok": True,
            "access_token": "at-browser",
            "refresh_token": "",
            "session_token": "st",
            "id_token": "",
            "password": "Passw0rd!",
        })
        service._run_oauth_browser = MagicMock(return_value={
            "ok": True,
            "callback_url": "http://localhost:1455/auth/callback?code=abc&state=st",
        })
        return service

    async def test_requires_proxy(self):
        service = self._service()
        onboard_mod, settings_service, originals = self._patch()
        try:
            result = await service.register_free_account(self.session, email_line="free@icloud.com")
        finally:
            self._restore(onboard_mod, settings_service, originals)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "proxy_missing")
        service._run_browser.assert_not_called()

    async def test_refuses_team_seat(self):
        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            account_id="acc-1",
            max_members=5,
            current_members=2,
            proxy="socks5h://127.0.0.1:1080",
            status="active",
            account_role="account-owner",
        )
        self.session.add(team)
        await self.session.flush()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com", password="Passw0rd!")
        await child_account_service.mark_active(self.session, child, team)
        service = self._service()
        result = await service.register_free_account(
            self.session,
            email_line="kid@example.com",
            proxy="socks5h://127.0.0.1:1080",
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "already_on_team")
        service._run_browser.assert_not_called()

    async def test_register_oauth_and_push_free_template(self):
        service = self._service()
        onboard_mod, settings_service, originals = self._patch()
        try:
            result = await service.register_free_account(
                self.session,
                email_line="free@icloud.com",
                phone_line="+15551234567----https://sms.example/key",
                proxy="socks5h://127.0.0.1:1080",
            )
            import_mock = onboard_mod.sub2api_service.import_session
        finally:
            self._restore(onboard_mod, settings_service, originals)
        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "free")
        self.assertTrue(result["oauth"])
        service._run_browser.assert_not_called()
        service._run_oauth_browser.assert_called_once()
        import_mock.assert_awaited_once()
        kwargs = import_mock.await_args.kwargs
        self.assertEqual(kwargs["name_style"], "free")
        self.assertIsNone(kwargs["team"])
        child = await child_account_service.get_by_email(self.session, "free@icloud.com")
        self.assertEqual(child.status, "free")
        self.assertEqual(child.sub2api_account_id, 77)
        self.assertEqual(child_account_service.decrypt_secret(child.refresh_token_encrypted), "rt-oauth")


if __name__ == "__main__":
    unittest.main()
