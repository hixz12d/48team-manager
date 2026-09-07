import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application import console_actions
from app.application.replenish import ReplenishService
from app.application.resources.hme import HmeConfig
from app.application.tokens import encrypt_secret
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace
from app.persistence.models.operations import Operation


WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"
PROXY = "socks5h://127.0.0.1:1080"


class ReplenishTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _seed_workspace(self, *, seat_limit=5, occupied=1, proxy=PROXY) -> Workspace:
        owner = Account(
            email="owner@example.com",
            official_plan="unknown",
            local_purpose="mother",
            operational_state="active",
            access_token_encrypted=encrypt_secret("tok"),
            proxy=proxy,
        )
        self.session.add(owner)
        await self.session.flush()
        workspace = Workspace(
            official_workspace_id=WORKSPACE_UUID,
            owner_account_id=owner.id,
            status="active",
            seat_limit=seat_limit,
            occupied_seats=occupied,
            name="Team .2026.12",
        )
        self.session.add(workspace)
        await self.session.commit()
        return workspace

    async def test_team_full_stops_before_onboard(self):
        workspace = await self._seed_workspace(seat_limit=5, occupied=5)
        onboard = AsyncMock(side_effect=AssertionError("onboard must not run"))
        reauth = AsyncMock()
        service = ReplenishService(onboard=type("Onb", (), {"invite_and_onboard": onboard})(), reauth=reauth)
        with patch("app.application.replenish.hme_service.load_config", new=AsyncMock(return_value=HmeConfig(base_url="http://hme", service_token="t"))):
            result = await service.run(self.session, workspace_id=workspace.id, in_test=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "team_full")
        onboard.assert_not_called()

    async def test_hme_unconfigured_stops_before_onboard(self):
        workspace = await self._seed_workspace()
        onboard = AsyncMock(side_effect=AssertionError("onboard must not run"))
        service = ReplenishService(onboard=type("Onb", (), {"invite_and_onboard": onboard})(), reauth=AsyncMock())
        with patch("app.application.replenish.hme_service.load_config", new=AsyncMock(return_value=HmeConfig())):
            result = await service.run(self.session, workspace_id=workspace.id, in_test=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "hme_unconfigured")
        onboard.assert_not_called()

    async def test_onboard_then_immediate_reauth_marks_usable(self):
        workspace = await self._seed_workspace()
        child = Account(
            email="alias@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            proxy=PROXY,
        )
        self.session.add(child)
        await self.session.commit()
        onboard = AsyncMock(
            return_value={"success": True, "status": "active", "child": {"id": child.id, "email": child.email}, "pushed": False}
        )
        reauth = AsyncMock()
        reauth.run_immediate_reauth = AsyncMock(return_value={"success": True, "status": "success"})
        service = ReplenishService(onboard=type("Onb", (), {"invite_and_onboard": onboard})(), reauth=reauth)
        with (
            patch("app.application.replenish.hme_service.load_config", new=AsyncMock(return_value=HmeConfig(base_url="http://hme", service_token="t"))),
            patch("app.application.replenish.probe_account_mailbox", new=AsyncMock(return_value={"ok": True})),
        ):
            result = await service.run(self.session, workspace_id=workspace.id, in_test=True)
        self.assertTrue(result["success"])
        self.assertEqual(result["email"], "alias@icloud.com")
        self.assertFalse(result.get("pushed"))
        onboard.assert_awaited_once()
        self.assertEqual(onboard.await_args.kwargs["email_line"], "")
        self.assertEqual(onboard.await_args.kwargs.get("phone_line"), "")
        reauth.run_immediate_reauth.assert_awaited_once()
        await self.session.refresh(child)
        self.assertTrue(child.auto_reauth_opt_in)

    async def test_auth_failure_after_join_is_partial_not_new_claim(self):
        workspace = await self._seed_workspace()
        child = Account(
            email="alias@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            proxy=PROXY,
        )
        self.session.add(child)
        await self.session.commit()
        onboard = AsyncMock(
            return_value={"success": True, "status": "active", "child": {"id": child.id, "email": child.email}}
        )
        reauth = AsyncMock()
        reauth.run_immediate_reauth = AsyncMock(
            return_value={"success": False, "skipped": True, "error_code": "reauth_manual", "error": "没有邮箱读码配置"}
        )
        service = ReplenishService(onboard=type("Onb", (), {"invite_and_onboard": onboard})(), reauth=reauth)
        with (
            patch("app.application.replenish.hme_service.load_config", new=AsyncMock(return_value=HmeConfig(base_url="http://hme", service_token="t"))),
            patch("app.application.replenish.probe_account_mailbox", new=AsyncMock(return_value={"ok": False})),
        ):
            result = await service.run(self.session, workspace_id=workspace.id, in_test=True)
        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["error_code"], "reauth_manual")
        self.assertEqual(result["account_id"], child.id)
        self.assertIn("不要再领新号", result["message"])

    async def test_console_replenish_missing_workspace(self):
        result = await console_actions.start_workspace_replenish(self.session, 999)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "not_found")
        count = list((await self.session.execute(select(Operation))).scalars())
        self.assertEqual(len(count), 0)

    async def test_replenish_passes_phone_line_into_onboard(self):
        workspace = await self._seed_workspace()
        child = Account(
            email="alias@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            proxy=PROXY,
        )
        self.session.add(child)
        await self.session.commit()
        phone_line = "+17822063428----https://api668.com/sms/by_key?key=test"
        onboard = AsyncMock(
            return_value={"success": True, "status": "active", "child": {"id": child.id, "email": child.email}, "pushed": False}
        )
        reauth = AsyncMock()
        reauth.run_immediate_reauth = AsyncMock(return_value={"success": True, "status": "success"})
        service = ReplenishService(onboard=type("Onb", (), {"invite_and_onboard": onboard})(), reauth=reauth)
        with (
            patch("app.application.replenish.hme_service.load_config", new=AsyncMock(return_value=HmeConfig(base_url="http://hme", service_token="t"))),
            patch("app.application.replenish.probe_account_mailbox", new=AsyncMock(return_value={"ok": True})),
        ):
            result = await service.run(self.session, workspace_id=workspace.id, phone_line=phone_line, in_test=True)
        self.assertTrue(result["success"])
        self.assertEqual(onboard.await_args.kwargs["phone_line"], phone_line)

    async def test_bind_account_phone_stores_number(self):
        child = Account(
            email="alias@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            proxy=PROXY,
        )
        self.session.add(child)
        await self.session.commit()
        result = await console_actions.bind_account_phone(
            self.session,
            child.id,
            "+13657403674----https://api668.com/sms/by_key?key=test",
        )
        self.assertTrue(result["ok"])
        await self.session.refresh(child)
        self.assertEqual(child.phone, "+13657403674")
        self.assertTrue(str(child.sms_url).startswith("https://api668.com/sms/by_key"))


if __name__ == "__main__":
    unittest.main()
