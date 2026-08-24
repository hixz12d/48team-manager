import unittest
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
