import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application import console_actions
from app.application.replenish import ReplenishService
from app.application.resources.hme import HmeConfig
from app.application.tokens import encrypt_secret
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace
from app.persistence.models.operations import Operation


class ReplenishTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.session = async_sessionmaker(self.engine, expire_on_commit=False)()
        owner = Account(email="owner@example.com", local_purpose="mother", proxy="socks5h://127.0.0.1:1080", access_token_encrypted=encrypt_secret("token"))
        self.session.add(owner)
        await self.session.flush()
        self.workspace = Workspace(owner_account_id=owner.id, status="active", name="Test Team", seat_limit=2)
        self.session.add(self.workspace)
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_unified_flow_is_called_once_without_second_reauth(self):
        child = Account(email="child@example.com", local_purpose="child", operational_state="active")
        self.session.add(child)
        await self.session.commit()
        onboard = AsyncMock()
        onboard.invite_and_onboard.return_value = {"success": True, "authorized": True, "child": {"id": child.id, "email": child.email}}
        reauth = AsyncMock()
        result = await ReplenishService(onboard=onboard, reauth=reauth).run(self.session, workspace_id=self.workspace.id, role="owner", seat_intent="premium")
        self.assertTrue(result["success"])
        self.assertFalse(result["pushed"])
        self.assertTrue(onboard.invite_and_onboard.await_args.kwargs["oauth_signup"])
        self.assertEqual(onboard.invite_and_onboard.await_args.kwargs["seat_intent"], "premium")
        reauth.run_immediate_reauth.assert_not_called()
        self.assertFalse(child.auto_reauth_opt_in)

    async def test_partial_result_keeps_same_child_and_no_new_claim(self):
        onboard = AsyncMock()
        onboard.invite_and_onboard.return_value = {
            "success": False, "partial": True, "joined": True, "authorized": False,
            "error_code": "phone_verification_required", "child": {"id": 44, "email": "same@example.com"},
        }
        result = await ReplenishService(onboard=onboard).run(self.session, workspace_id=self.workspace.id)
        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["account_id"], 44)
        self.assertEqual(result["email"], "same@example.com")
        onboard.invite_and_onboard.assert_awaited_once()

    async def test_phone_line_is_only_forwarded_as_explicit_input(self):
        onboard = AsyncMock()
        onboard.invite_and_onboard.return_value = {"success": False, "error_code": "sms_failed"}
        line = "+15555555555----https://sms.example/receipt?key=test"
        result = await ReplenishService(onboard=onboard).run(self.session, workspace_id=self.workspace.id, phone_line=line)
        self.assertEqual(onboard.invite_and_onboard.await_args.kwargs["phone_line"], line)
        self.assertNotIn(line, str(result))

    async def test_team_full_stops_before_claim(self):
        from app.application.onboard import OnboardService
        service = OnboardService()
        live = ({"success": True, "members": [{"email": "owner@example.com"}, {"email": "other@example.com"}]}, None)
        with (
            patch.object(service.workspaces, "lookup_live_member", new=AsyncMock(return_value=live)),
            patch("app.application.invitation_flow.load_cf_config", new=AsyncMock(return_value={"base_url": "https://mail.example", "address": "mail@example.com", "admin_password": "test"})),
            patch("app.application.resources.hme.claim_next_alias", new=AsyncMock()) as claim,
        ):
            result = await ReplenishService(onboard=service).run(self.session, workspace_id=self.workspace.id)
        self.assertEqual(result["error_code"], "team_full")
        claim.assert_not_awaited()

    async def test_hme_unconfigured_stops_before_invitation(self):
        from app.application.onboard import OnboardService
        service = OnboardService()
        with (
            patch.object(service.workspaces, "lookup_live_member", new=AsyncMock(return_value=({"success": True, "members": []}, None))),
            patch("app.application.invitation_flow.load_cf_config", new=AsyncMock(return_value={"base_url": "https://mail.example", "address": "mail@example.com", "admin_password": "test"})),
            patch("app.application.resources.hme.load_config", new=AsyncMock(return_value=HmeConfig())),
        ):
            result = await ReplenishService(onboard=service).run(self.session, workspace_id=self.workspace.id)
        self.assertEqual(result["error_code"], "hme_unconfigured")

    async def test_console_replenish_missing_workspace(self):
        result = await console_actions.start_workspace_replenish(self.session, 999)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "not_found")
        self.assertEqual(list((await self.session.execute(select(Operation))).scalars()), [])

    async def test_bind_account_phone_stores_explicit_binding(self):
        child = Account(email="alias@icloud.com", local_purpose="child")
        self.session.add(child)
        await self.session.commit()
        result = await console_actions.bind_account_phone(self.session, child.id, "+15555555555----https://sms.example/receipt?key=test")
        self.assertTrue(result["ok"])
        await self.session.refresh(child)
        self.assertEqual(child.phone, "+15555555555")

    async def test_conditional_sms_receipt_is_not_stored_in_plain_operation_fields(self):
        line = "+15555555555----https://sms.example/receipt?key=private-test-key"
        with patch.object(console_actions.replenish_service, "run", new=AsyncMock(return_value={"success": False, "error_code": "sms_failed"})):
            await console_actions.start_workspace_replenish(self.session, self.workspace.id, phone_line=line)
        operation = await self.session.scalar(select(Operation))
        self.assertEqual(operation.phone, "")
        for value in (operation.input_json, operation.log_json, operation.result_json):
            self.assertNotIn("private-test-key", value or "")
            self.assertNotIn("https://sms.example", value or "")


if __name__ == "__main__":
    unittest.main()
