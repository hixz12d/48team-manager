"""Seat selection contracts. All upstream and browser boundaries are mocked."""

import json
import socket
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from app.application import console_actions
from app.application.console_maintenance import add_local_child
from app.application.onboard import OnboardService
from app.application.replenish import ReplenishService
from app.application.resources.hme import HmeConfig
from app.application.workspaces import WorkspaceService
from app.integrations.openai.chatgpt import ChatGPTClient
from app.integrations.openai.member_adapter import existing_invite_seat_error
from app.persistence.models.operations import Operation
from app.web.schemas.resources import OnboardRequest, ReplenishRequest, WorkspaceAddChildRequest
from tests.helpers import make_client
from tests.test_onboard import OnboardTests, _FakeChatGPT


class SeatFlowTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = OnboardTests.asyncSetUp
    asyncTearDown = OnboardTests.asyncTearDown
    _seed_workspace = OnboardTests._seed_workspace

    def network_guard(self):
        stack = ExitStack()
        stack.enter_context(patch.object(socket.socket, "connect", side_effect=AssertionError("Network forbidden")))
        stack.enter_context(patch("curl_cffi.requests.AsyncSession.request", new=AsyncMock(side_effect=AssertionError("HTTP forbidden"))))
        return stack

    async def test_console_to_wire_all_seats_and_roles_without_real_requests(self):
        workspace = await self._seed_workspace()
        client = ChatGPTClient()
        captured = []

        async def request(method, url, headers, **kwargs):
            if method == "GET":
                return {"success": True, "data": {"items": [], "total": 0}}
            self.assertEqual(method, "POST")
            captured.append(kwargs["json_data"])
            return {"success": False, "status_code": 422, "error": "fixture stop", "error_code": "fixture_stop"}

        client._make_request = AsyncMock(side_effect=request)
        browser = AsyncMock(side_effect=AssertionError("Browser forbidden"))
        service = OnboardService(workspaces=WorkspaceService(client=client), browser=browser)
        with self.network_guard(), patch.object(console_actions, "onboard_service", service), patch(
            "app.application.onboard.hme_service.maybe_claim_alias",
            new=AsyncMock(return_value=("seat@example.test", None)),
        ), patch("app.application.onboard.hme_service.finalize_claim", new=AsyncMock()):
            for seat, wire in (("workspace_default", None), ("standard", "default"), ("premium", "prolite")):
                for role, role_wire in (("owner", "account-owner"), ("member", "standard-user")):
                    with self.subTest(seat=seat, role=role):
                        result = await console_actions.start_workspace_onboard(
                            self.session, workspace.id, email_line="seat@example.test", seat_intent=seat, role=role,
                        )
                        self.assertFalse(result["ok"])
                        self.assertEqual(result["error_code"], "fixture_stop")
                        self.assertEqual(captured[-1]["role"], role_wire)
                        self.assertEqual(captured[-1].get("seat_type"), wire)
                        if wire is None:
                            self.assertNotIn("seat_type", captured[-1])
                        operation = await self.session.scalar(select(Operation).order_by(Operation.id.desc()).limit(1))
                        self.assertEqual(json.loads(operation.input_json)["seat_intent"], seat)
            self.assertEqual(len(captured), 6)
            browser.assert_not_called()

    async def test_replenish_preserves_seat_through_operation_and_service(self):
        workspace = await self._seed_workspace()
        onboard = AsyncMock()
        onboard.invite_and_onboard = AsyncMock(return_value={"success": False, "error_code": "fixture_stop"})
        service = ReplenishService(onboard=onboard, reauth=AsyncMock())
        with self.network_guard(), patch.object(console_actions, "replenish_service", service):
            await console_actions.start_workspace_replenish(self.session, workspace.id, seat_intent="premium", role="member")
        self.assertEqual(onboard.invite_and_onboard.await_args.kwargs["seat_intent"], "premium")
        self.assertEqual(onboard.invite_and_onboard.await_args.kwargs["role"], "member")
        operation = await self.session.scalar(select(Operation))
        self.assertEqual(json.loads(operation.input_json)["seat_intent"], "premium")

    async def test_reinvite_preserves_seat_in_operation(self):
        workspace = await self._seed_workspace()
        mock = AsyncMock(return_value={"ok": False, "error_code": "fixture_stop"})
        with self.network_guard(), patch.object(console_actions, "add_local_child", mock):
            await console_actions.invite_workspace_child(self.session, workspace.id, email="seat@example.test", seat_intent="premium")
        self.assertEqual(mock.await_args.kwargs["seat_intent"], "premium")
        operation = await self.session.scalar(select(Operation))
        self.assertEqual(json.loads(operation.input_json)["seat_intent"], "premium")

    async def test_existing_seat_mismatch_stops_before_send_and_browser(self):
        workspace = await self._seed_workspace()
        for observed in ("default", None, "future-seat"):
            client = _FakeChatGPT()
            client.get_invites = AsyncMock(return_value={"success": True, "total": 1, "items": [
                {"email_address": "seat@example.test", "role": "account-owner", "seat_type": observed}
            ]})
            workspaces = WorkspaceService(client=client)
            browser = AsyncMock(side_effect=AssertionError("Browser forbidden"))
            onboard = OnboardService(workspaces=workspaces, browser=browser)
            with self.network_guard():
                result = await onboard._invite_and_onboard_impl(
                    self.session, workspace_id=workspace.id, email_line="seat@example.test", seat_intent="premium", in_test=True,
                )
                maintenance = await add_local_child(self.session, workspace.id, email="seat@example.test", workspaces=workspaces, seat_intent="premium")
            expected = "invite_seat_mismatch" if observed == "default" else "invite_seat_unknown"
            self.assertEqual(result["error_code"], expected)
            self.assertEqual(maintenance["error_code"], expected)
            self.assertEqual(client.invites, [])
            browser.assert_not_called()

    async def test_manual_invite_passes_explicit_seat_to_workspace(self):
        workspace = await self._seed_workspace()
        workspaces = AsyncMock()
        workspaces.lookup_live_member.return_value = ({"success": True}, None)
        workspaces.invite_member.return_value = {"success": False, "error_code": "fixture_stop"}
        with self.network_guard():
            await add_local_child(self.session, workspace.id, email="seat@example.test", workspaces=workspaces, seat_intent="premium")
        self.assertEqual(workspaces.invite_member.await_args.kwargs["seat_intent"], "premium")


class SeatRequestTests(unittest.TestCase):
    def test_defaults_and_invalid_values(self):
        for model in (OnboardRequest, ReplenishRequest, WorkspaceAddChildRequest):
            kwargs = {"email": "seat@example.test"} if model is WorkspaceAddChildRequest else {}
            self.assertEqual(model(**kwargs).seat_intent, "workspace_default")
            for value in ("prolite", "invalid", "", None):
                with self.assertRaises(ValueError):
                    model(**kwargs, seat_intent=value)

    def test_existing_seat_contract(self):
        self.assertIsNone(existing_invite_seat_error("premium", "prolite"))
        self.assertIsNone(existing_invite_seat_error("standard", "default"))
        self.assertIsNone(existing_invite_seat_error("workspace_default", None))
        self.assertEqual(existing_invite_seat_error("premium", None)["error_code"], "invite_seat_unknown")

    def test_api_passes_seat_and_rejects_invalid_before_application(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            for endpoint, action, extra in (
                ("onboard", "start_workspace_onboard", {"email_line": "seat@example.test"}),
                ("replenish", "start_workspace_replenish", {}),
                ("members/add", "invite_workspace_child", {"email": "seat@example.test"}),
            ):
                with patch.object(console_actions, action, new=AsyncMock(return_value={"ok": True, "success": True})) as mocked:
                    response = client.post(f"/api/workspaces/1/{endpoint}", json={**extra, "seat_intent": "premium", "role": "member"})
                    self.assertIn(response.status_code, (200, 202))
                    self.assertEqual(mocked.await_args.kwargs["seat_intent"], "premium")
                    self.assertEqual(mocked.await_args.kwargs["role"], "member")
                    mocked.reset_mock()
                    response = client.post(f"/api/workspaces/1/{endpoint}", json={**extra, "seat_intent": "invalid"})
                    self.assertEqual(response.status_code, 422)
                    mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
