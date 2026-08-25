import unittest
from unittest.mock import AsyncMock
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import ChildAccount, Team
from app.services.child_accounts import child_account_service
from app.services.mail_otp import parse_mail_line
from app.services.sms import chrome_proxy_config, parse_phone_line, require_proxy
from app.utils.proxy import normalize_proxy_url
from app.utils.time_utils import get_now


class ChildAccountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_kick_keeps_child_as_standby(self):
        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            max_members=5,
            current_members=1,
            proxy="socks5h://127.0.0.1:1080",
            seat_cycle_days=7,
        )
        self.session.add(team)
        await self.session.flush()
        child = await child_account_service.upsert_from_input(
            self.session,
            email="kid@example.com",
            password="Secret123!",
            proxy="socks5h://127.0.0.1:1080",
        )
        await child_account_service.mark_active(self.session, child, team)
        await child_account_service.mark_standby(self.session, child)
        self.assertEqual(child.status, "standby")
        self.assertIsNone(child.current_team_id)
        self.assertEqual(child.last_team_id, team.id)
        self.assertTrue(child.password_encrypted)

    async def test_due_scan_uses_joined_at(self):
        team = Team(email="owner@example.com", access_token_encrypted="x", max_members=5)
        self.session.add(team)
        await self.session.flush()
        child = await child_account_service.upsert_from_input(self.session, email="old@example.com")
        await child_account_service.mark_active(self.session, child, team)
        child.joined_at = get_now() - timedelta(days=8)
        due = await child_account_service.list_due_accounts(self.session, team_id=team.id)
        self.assertEqual([item.email for item in due], ["old@example.com"])

    def test_workspace_member_errors_are_not_always_fatal(self):
        from app.services.team import TeamService

        self.assertFalse(TeamService._workspace_error_is_fatal({"error": "timeout"}))
        self.assertTrue(TeamService._workspace_error_is_fatal({"error_code": "account_deactivated"}))

    def test_remote_delete_success_includes_already_removed(self):
        from app.services.team import TeamService

        self.assertTrue(TeamService._remote_delete_succeeded({"success": True}))
        self.assertTrue(TeamService._remote_delete_succeeded({"success": False, "status_code": 404, "error": "not found"}))
        self.assertFalse(TeamService._remote_delete_succeeded({"success": False, "error": "timeout"}))

    async def test_delete_member_keeps_success_when_sync_fails(self):
        from app.services.team import TeamService

        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            account_id="acc-1",
            max_members=5,
            current_members=2,
        )
        self.session.add(team)
        await self.session.flush()

        service = TeamService()
        service.ensure_access_token = AsyncMock(return_value="tok")
        service.chatgpt_service = type("Svc", (), {})()
        service.chatgpt_service.delete_member = AsyncMock(
            return_value={"success": True, "status_code": 200, "error": None}
        )
        service.sync_team_info = AsyncMock(return_value={"success": False, "error": "rolled back"})
        service.mark_team_email_mapping_removed = AsyncMock()
        service._reset_error_status = AsyncMock()

        result = await service.delete_team_member(
            team.id, "user-abc", self.session, email="kid@example.com"
        )
        self.assertTrue(result["success"])
        self.assertIn("暂未刷新", result["message"])
        service.mark_team_email_mapping_removed.assert_awaited()

    async def test_sync_promotes_invited_when_live_joined(self):
        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            max_members=5,
            current_members=2,
            seat_cycle_days=7,
        )
        self.session.add(team)
        await self.session.flush()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        await child_account_service.mark_invited(self.session, child, team)
        joined_at = get_now() - timedelta(days=1)
        result = await child_account_service.sync_with_live_members(
            self.session,
            team,
            [{"email": "kid@example.com", "status": "joined", "added_at": joined_at.isoformat()}],
        )
        self.assertEqual(result["promoted"], 1)
        self.assertEqual(child.status, "active")
        self.assertEqual(child.joined_at.date(), joined_at.date())

    async def test_sync_releases_invited_when_live_absent(self):
        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            max_members=5,
            current_members=2,
        )
        self.session.add(team)
        await self.session.flush()
        child = await child_account_service.upsert_from_input(self.session, email="gone@example.com")
        await child_account_service.mark_invited(self.session, child, team)
        result = await child_account_service.sync_with_live_members(
            self.session,
            team,
            [{"email": "owner@example.com", "status": "joined", "role": "account-owner"}],
        )
        self.assertEqual(result["released"], 1)
        self.assertEqual(child.status, "unused")
        self.assertIsNone(child.current_team_id)

    async def test_full_error_does_not_shrink_max_members(self):
        from app.services.team import TeamService

        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            max_members=5,
            current_members=1,
            status="active",
        )
        self.session.add(team)
        await self.session.flush()
        service = TeamService()
        handled = await service._handle_api_error(
            {"success": False, "error": "Reached maximum number of seats"},
            team,
            self.session,
        )
        self.assertTrue(handled)
        self.assertEqual(team.max_members, 5)
        self.assertEqual(team.current_members, 1)
        self.assertEqual(team.status, "active")


class ParserTests(unittest.TestCase):
    def test_parse_phone_and_mail(self):
        number, url = parse_phone_line("+15551234567----https://api668.com/sms/by_key?key=abc")
        self.assertEqual(number, "+15551234567")
        self.assertTrue(url.startswith("https://"))
        parsed = parse_mail_line("a@icloud.com----https://pickup.example/show/x")
        self.assertEqual(parsed["email"], "a@icloud.com")
        self.assertTrue(parsed["pickup_url"].startswith("https://"))

    def test_require_proxy(self):
        self.assertEqual(normalize_proxy_url("127.0.0.1:1080:user:pass"), "socks5h://user:pass@127.0.0.1:1080")
        with self.assertRaises(ValueError):
            require_proxy("", "接码")
        self.assertEqual(
            chrome_proxy_config("socks5h://user:pass@127.0.0.1:1080"),
            {"server": "socks5://127.0.0.1:1080", "username": "user", "password": "pass"},
        )

    def test_proxy_check_rejects_empty(self):
        import asyncio
        from app.services.proxy_check import check_proxy
        result = asyncio.run(check_proxy(""))
        self.assertFalse(result["ok"])
        self.assertEqual(result["summary"], "未填写代理")


if __name__ == "__main__":
    unittest.main()
