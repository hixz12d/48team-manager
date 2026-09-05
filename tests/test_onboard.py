import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application import console_actions
from app.application.proxy_resolution import RuntimeProxy

from app.application.onboard import OnboardService
from app.application.rotate import RotateService
from app.application.tokens import encrypt_secret
from app.application.workspaces import WorkspaceService
from app.domain.vacancy import parse_policy_notice
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership
from app.persistence.models.operations import Operation


WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"
PROXY = "socks5h://127.0.0.1:1080"


class _FakeChatGPT:
    def __init__(self, *, auto_join: bool = False):
        self.invites = []
        self.members = []
        self.invite_error = None
        self.auto_join = auto_join

    async def send_invite(self, access_token, account_id, email, db_session, identifier="default", role="owner"):
        self.invites.append({"email": email, "role": role})
        if self.invite_error:
            return {"success": False, "error": self.invite_error, "error_code": "invite_failed"}
        if self.auto_join:
            self.members = [{"email": email, "id": "user-1", "role": "account-owner"}]
        return {"success": True, "data": {"ok": True}}

    async def get_members(self, access_token, account_id, db_session, identifier="default"):
        return {"success": True, "members": list(self.members), "total": len(self.members), "error": None}

    async def get_invites(self, access_token, account_id, db_session, identifier="default"):
        return {"success": True, "items": [], "total": 0, "error": None}


def _ok_browser(**kwargs):
    return {
        "ok": True,
        "access_token": "at",
        "refresh_token": "rt",
        "session_token": "st",
        "id_token": "id",
        "password": kwargs.get("password") or "Aa1!",
        "phone": kwargs.get("phone") or "",
        "sms_url": kwargs.get("sms_url") or "",
    }


class OnboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _seed_workspace(self) -> Workspace:
        owner = Account(
            email="owner@example.com",
            official_plan="unknown",
            local_purpose="mother",
            operational_state="active",
            access_token_encrypted=encrypt_secret("tok"),
            proxy=PROXY,
        )
        self.session.add(owner)
        await self.session.flush()
        workspace = Workspace(
            official_workspace_id=WORKSPACE_UUID,
            owner_account_id=owner.id,
            status="active",
            seat_limit=5,
            name="Team .2026.12",
        )
        self.session.add(workspace)
        await self.session.flush()
        return workspace

    def _service(self, client, browser=_ok_browser):
        workspaces = WorkspaceService(client=client)
        return OnboardService(workspaces=workspaces, browser=browser)

    async def test_invite_onboard_joins_without_pushing_sub2api(self):
        workspace = await self._seed_workspace()
        client = _FakeChatGPT(auto_join=True)
        service = self._service(client)
        result = await service.invite_and_onboard(
            self.session,
            workspace_id=workspace.id,
            email_line="kid@icloud.com----https://mail.example/pickup",
            proxy=PROXY,
            proxy_source="sub2api",
            sub2api_proxy_id=7,
            proxy_instance_key="instance-key",
            in_test=True,
        )
        self.assertTrue(result["success"])
        self.assertEqual(result["child"]["email"], "kid@icloud.com")
        self.assertFalse(result.get("pushed"))
        self.assertEqual(client.invites, [{"email": "kid@icloud.com", "role": "owner"}])
        child = (await self.session.get(Account, result["child"]["id"]))
        self.assertEqual(child.operational_state, "active")
        self.assertEqual(child.proxy_source, "sub2api")
        self.assertEqual(child.sub2api_proxy_id, 7)
        self.assertEqual(child.proxy_instance_key, "instance-key")
        membership = (
            await self.session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.account_id == child.id)
            )
        ).scalar_one_or_none()
        self.assertIsNotNone(membership)
        self.assertEqual(membership.official_role, "owner")

    async def test_console_onboard_resolves_sub2api_id_without_persisting_secret_input(self):
        workspace = await self._seed_workspace()
        resolved = RuntimeProxy(
            source="sub2api",
            remote_id=7,
            instance_key="instance-key",
            url="socks5h://user:secret@127.0.0.1:1080",
        )
        onboard = AsyncMock(return_value={"success": True, "status": "success"})
        with (
            patch("app.application.console_actions.resolve_sub2api_proxy", new=AsyncMock(return_value=resolved)),
            patch.object(console_actions.onboard_service, "invite_and_onboard", new=onboard),
        ):
            result = await console_actions.start_workspace_onboard(
                self.session,
                workspace.id,
                email_line="kid@icloud.com----https://mail.example/pickup",
                proxy_selection={"source": "sub2api", "remote_id": 7},
            )
        self.assertTrue(result["ok"])
        self.assertEqual(onboard.await_args.kwargs["proxy"], resolved.url)
        self.assertEqual(onboard.await_args.kwargs["sub2api_proxy_id"], 7)
        operation = await self.session.get(Operation, 1)
        self.assertNotIn("secret", operation.input_json)
        self.assertIn('\"sub2api_proxy_id\": 7', operation.input_json)

    async def test_empty_email_claims_hme_then_onboards(self):
        workspace = await self._seed_workspace()
        client = _FakeChatGPT(auto_join=True)
        claimed = type("Claim", (), {"email": "alias@icloud.com", "lease_id": 1, "account_id": "hme", "anonymous_id": "a"})()
        service = self._service(client)
        service_hme = __import__("app.application.resources.hme", fromlist=["hme"])
        orig_claim = service_hme.maybe_claim_alias
        orig_final = service_hme.finalize_claim
        orig_start = service_hme.mark_signup_started

        async def fake_claim(*args, **kwargs):
            return "alias@icloud.com----https://mail.example/pickup", claimed

        async def fake_final(*args, **kwargs):
            return None

        async def fake_start(*args, **kwargs):
            return None

        service_hme.maybe_claim_alias = fake_claim
        service_hme.finalize_claim = fake_final
        service_hme.mark_signup_started = fake_start
        try:
            result = await service.invite_and_onboard(
                self.session,
                workspace_id=workspace.id,
                email_line="",
                in_test=True,
            )
        finally:
            service_hme.maybe_claim_alias = orig_claim
            service_hme.finalize_claim = orig_final
            service_hme.mark_signup_started = orig_start
        self.assertTrue(result["success"])
        self.assertEqual(result["child"]["email"], "alias@icloud.com")
        self.assertEqual(client.invites, [{"email": "alias@icloud.com", "role": "owner"}])

    async def test_invite_failure_stops_before_browser(self):
        workspace = await self._seed_workspace()
        client = _FakeChatGPT()
        client.invite_error = "forbidden"
        browser = AsyncMock(side_effect=AssertionError("browser must not run"))
        service = self._service(client, browser=browser)
        result = await service.invite_and_onboard(
            self.session,
            workspace_id=workspace.id,
            email_line="kid@icloud.com----https://mail.example/pickup",
            in_test=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "invite_failed")
        browser.assert_not_called()

    async def test_skip_invite_does_not_call_invite_member(self):
        workspace = await self._seed_workspace()
        client = _FakeChatGPT()
        service = self._service(client)
        result = await service.invite_and_onboard(
            self.session,
            workspace_id=workspace.id,
            email_line="kid@icloud.com----https://mail.example/pickup",
            skip_invite=True,
            in_test=True,
        )
        self.assertEqual(client.invites, [])
        self.assertNotEqual(result.get("error_code"), "invite_failed")
        membership = (
            await self.session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.account_id == result.get("child", {}).get("id"))
            )
        ).scalar_one_or_none()
        if membership is not None:
            self.assertNotEqual(membership.membership_state, "invited")

    async def test_not_joined_does_not_mark_active(self):
        workspace = await self._seed_workspace()
        client = _FakeChatGPT()
        service = self._service(client)
        result = await service.invite_and_onboard(
            self.session,
            workspace_id=workspace.id,
            email_line="kid@icloud.com----https://mail.example/pickup",
            in_test=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "not_joined")
        child = (await self.session.get(Account, result["child"]["id"]))
        self.assertNotEqual(child.operational_state, "active")

    async def test_rotate_default_refill_uses_onboard(self):
        workspace = await self._seed_workspace()
        child = Account(
            email="old@icloud.com",
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
        )
        self.session.add(child)
        await self.session.flush()
        self.session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                account_id=child.id,
                official_role="member",
                membership_state="joined",
                local_purpose="child",
            )
        )
        await self.session.commit()
        vacancy = parse_policy_notice({"policy_notice": {"vacancy_ordinal": 1, "free_vacancy_threshold": 5}})
        onboard = type("Onb", (), {})()
        onboard.refill = AsyncMock(return_value={"success": True, "child": {"email": "new@icloud.com"}})
        service = RotateService(onboard=onboard)
        service.kick_to_standby = AsyncMock(return_value={"success": True, "status": "standby", "vacancy": vacancy})
        result = await service.kick_and_refill(
            self.session,
            workspace_id=workspace.id,
            email=child.email,
            in_test=True,
        )
        self.assertTrue(result["success"])
        onboard.refill.assert_awaited_once()
        kwargs = onboard.refill.await_args.kwargs
        self.assertEqual(kwargs["skip_email"], child.email)
        self.assertEqual(kwargs.get("role"), "owner")
        self.assertTrue(kwargs["in_test"])


if __name__ == "__main__":
    unittest.main()
