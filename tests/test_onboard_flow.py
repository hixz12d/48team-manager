import inspect
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import Team
from app.services.child_accounts import child_account_service
from app.services.browser_onboard import can_peek_session, looks_like_cloudflare, session_access_token
from app.services.onboard import OnboardService, classify_onboard_error
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


if __name__ == "__main__":
    unittest.main()
