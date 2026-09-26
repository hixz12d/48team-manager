"""Batch 4: workspace commands return an operation id at once and run in the background."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import select

from app.application import console_actions
from app.persistence.models.identity import Account, Workspace
from app.persistence.models.operations import Operation
from tests.helpers import make_client


class AsyncCommandApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._ctx = make_client(Path(self._tmp.name))
        self.client = self._ctx.__enter__()
        self.client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
        self.workspace_id = self._run(self._seed())

    def tearDown(self):
        self._ctx.__exit__(None, None, None)
        self._tmp.cleanup()

    def _run(self, coro):
        return self.client.portal.call(lambda: coro)

    async def _seed(self):
        async with self.client.app.state.session_factory() as db:
            owner = Account(email="owner@example.com", official_plan="unknown", local_purpose="mother", operational_state="active")
            db.add(owner)
            await db.flush()
            workspace = Workspace(official_workspace_id="ws-1", owner_account_id=owner.id, status="active", seat_limit=5, name="North")
            db.add(workspace)
            await db.commit()
            return workspace.id

    def _wait(self, operation_id, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            detail = self.client.get(f"/api/operations/{operation_id}").json()
            if detail["state"] not in {"queued", "running", "waiting"}:
                return detail
            time.sleep(0.05)
        self.fail(f"operation {operation_id} did not finish")

    def test_each_command_returns_running_operation_before_the_flow_ends(self):
        gate = {}
        cases = (
            ("onboard", "onboard_service", "invite_and_onboard", {"email_line": "kid@example.com"}),
            ("replenish", "replenish_service", "run", {}),
            ("rotate", "rotate_service", "run_rotate_saga", {"email": "kid@example.com"}),
            ("kick", "rotate_service", "kick_to_standby", {"email": "kid@example.com"}),
        )
        for endpoint, service, method, body in cases:
            with self.subTest(endpoint=endpoint):
                release = gate[endpoint] = None

                async def slow(*args, _endpoint=endpoint, **kwargs):
                    event = gate[_endpoint] = asyncio.Event()
                    await event.wait()
                    return {"success": True, "status": "success", "child": {"email": "kid@example.com"}}

                with patch.object(getattr(console_actions, service), method, new=slow):
                    started = time.monotonic()
                    response = self.client.post(f"/api/workspaces/{self.workspace_id}/{endpoint}", json=body)
                    self.assertLess(time.monotonic() - started, 2.0)
                    self.assertEqual(response.status_code, 202)
                    payload = response.json()
                    self.assertTrue(payload["accepted"])
                    self.assertEqual(payload["status"], "running")
                    operation_id = payload["operation_id"]
                    detail = self.client.get(f"/api/operations/{operation_id}").json()
                    self.assertEqual(detail["state"], "running")
                    runtime = self.client.get("/api/runtime/status").json()
                    self.assertIn(operation_id, [item["id"] for item in runtime["active_operations"]])
                    for _ in range(100):
                        if gate.get(endpoint) is not None:
                            break
                        time.sleep(0.02)
                    release = gate[endpoint]
                    self.client.portal.call(release.set)
                    finished = self._wait(operation_id)
                self.assertEqual(finished["state"], "success")
                self.assertEqual(finished["result"]["child"]["email"], "kid@example.com")

    def test_second_command_on_same_team_conflicts_with_the_running_one(self):
        event_holder = {}

        async def slow(*args, **kwargs):
            event = event_holder["event"] = asyncio.Event()
            await event.wait()
            return {"success": True, "status": "success"}

        with patch.object(console_actions.onboard_service, "invite_and_onboard", new=slow):
            first = self.client.post(f"/api/workspaces/{self.workspace_id}/onboard", json={"email_line": "a@example.com"}).json()
            second = self.client.post(f"/api/workspaces/{self.workspace_id}/kick", json={"email": "b@example.com"}).json()
            rotate = self.client.post(f"/api/workspaces/{self.workspace_id}/rotate", json={"email": "b@example.com"}).json()
            self.assertEqual(second["error_code"], "operation_conflict")
            self.assertEqual(second["operation_id"], first["operation_id"])
            self.assertEqual(rotate["error_code"], "operation_conflict")
            self.assertEqual(rotate["operation_id"], first["operation_id"])
            for _ in range(100):
                if "event" in event_holder:
                    break
                time.sleep(0.02)
            self.client.portal.call(event_holder["event"].set)
            self._wait(first["operation_id"])

    def test_unexpected_error_is_recorded_without_leaking_details(self):
        async def boom(*args, **kwargs):
            raise RuntimeError("token=secret-value")

        with patch.object(console_actions.replenish_service, "run", new=boom):
            payload = self.client.post(f"/api/workspaces/{self.workspace_id}/replenish", json={}).json()
            detail = self._wait(payload["operation_id"])
        self.assertEqual(detail["state"], "failed")
        self.assertEqual(detail["error_code"], "command_failed")
        self.assertNotIn("secret-value", str(detail))
        # Lock is released: a new command on the same team is accepted.
        with patch.object(console_actions.replenish_service, "run", new=lambda *a, **k: _done()):
            again = self.client.post(f"/api/workspaces/{self.workspace_id}/replenish", json={}).json()
            self.assertTrue(again["accepted"])
            self._wait(again["operation_id"])

    def test_flow_cancellation_at_a_safe_point_is_recorded_as_cancelled(self):
        async def cancelled(*args, **kwargs):
            raise asyncio.CancelledError()

        with patch.object(console_actions.onboard_service, "invite_and_onboard", new=cancelled):
            payload = self.client.post(f"/api/workspaces/{self.workspace_id}/onboard", json={"email_line": "a@example.com"}).json()
            detail = self._wait(payload["operation_id"])
        self.assertEqual(detail["state"], "cancelled")


class AsyncCommandShutdownTests(unittest.TestCase):
    """Separate class: the global dispatchers cannot span two live apps."""

    def test_shutdown_marks_running_command_for_manual_review(self):
        async def forever(*args, **kwargs):
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as tmp:
            with make_client(Path(tmp)) as client:
                client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
                holder = AsyncCommandApiTests()
                holder.client = client
                workspace_id = client.portal.call(holder._seed)
                with patch.object(console_actions.onboard_service, "invite_and_onboard", new=forever):
                    operation_id = client.post(f"/api/workspaces/{workspace_id}/onboard", json={"email_line": "a@example.com"}).json()["operation_id"]
                    self.assertEqual(client.get(f"/api/operations/{operation_id}").json()["state"], "running")
            with make_client(Path(tmp)) as client:

                async def read():
                    async with client.app.state.session_factory() as db:
                        return (await db.execute(select(Operation).where(Operation.public_id == operation_id))).scalar_one()

                row = client.portal.call(read)
            self.assertEqual(row.state, "manual_required")
            self.assertEqual(row.error_code, "resume_manual")


async def _done():
    return {"success": True, "status": "success"}


if __name__ == "__main__":
    unittest.main()
