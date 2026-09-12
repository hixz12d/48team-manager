"""Isolated remote-state observations, identity checks and follow-up retries."""
import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx
import tests.test_sub2api_management as management
from app.application.sub2api_remote_state import cached_remote_state, refresh_remote_state, retry_remote_followups
from app.integrations.sub2api.client import Sub2ApiClient
from app.integrations.sub2api.sync_state import parse_state, request_state, retry_followups
from app.persistence.models.sub2api import Sub2ApiSyncObservation

INSTANCE = "9c365a68-bd8d-42be-9b65-8f881b38e91a"
STAMP = "2026-09-11T12:00:00+00:00"


def fixture(email="billing@example.com", state="pending"):
    step = "succeeded" if state == "completed" else "pending"
    return {"schema_version": 1, "instance_id": INSTANCE, "remote_account_id": 42, "platform": "openai", "type": "oauth",
            "identity": {"email": email, "workspace_id": "", "official_account_id": "acct-billing"},
            "credential_version": 7, "account_updated_at": STAMP, "observed_at": STAMP,
            "schedulable": False, "status": "error", "remaining_blockers": ["schedulable_off", "rate_limit"],
            "latest_operation": {"remote_account_id": 42, "operation_id": "fixture-operation", "credential_version": 7,
                "credential_write": "succeeded", "state": state, "token_cache_invalidation": step, "scheduler_refresh": step,
                "updated_at": STAMP, "created_at": STAMP, "next_attempt_at": STAMP, "last_error": ""}}


class RemoteStatePersistenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = management.Sub2ApiManagementTests.asyncSetUp
    asyncTearDown = management.Sub2ApiManagementTests.asyncTearDown

    async def refresh(self, value):
        with patch("app.application.sub2api_remote_state.request_state", new=AsyncMock(return_value=value)):
            return await refresh_remote_state(self.session, self.account.id)

    async def test_pin_instance_redact_and_keep_snapshot_on_failure(self):
        raw = fixture(self.account.email)
        raw["credentials"] = {"access_token": "fixture-secret"}
        result = await self.refresh({"ok": True, "snapshot": raw})
        self.assertTrue(result["can_retry"])
        self.assertEqual(result["state"], "pending")
        async with self.session_maker() as fresh:
            row = await fresh.get(Sub2ApiSyncObservation, self.binding.id)
            self.assertEqual(row.instance_id, INSTANCE)
            self.assertNotIn("fixture-secret", row.snapshot_json)
        failed = await self.refresh({"ok": False, "error_code": "bridge_unavailable"})
        self.assertFalse(failed["ok"])
        self.assertTrue(failed["stale"])
        self.assertEqual(failed["snapshot"], result["snapshot"])
        self.assertEqual(failed["checked_at"], result["checked_at"])
        self.assertFalse(failed["can_retry"])

    async def test_instance_and_identity_drift_do_not_replace_verified_snapshot(self):
        baseline = await self.refresh({"ok": True, "snapshot": fixture()})
        for field in ("instance", "email", "workspace", "official"):
            changed = fixture()
            if field == "instance": changed["instance_id"] = "b69d02db-8fb2-4f79-bef2-482476b1c100"
            if field == "email": changed["identity"]["email"] = "someone@example.invalid"
            if field == "workspace": changed["identity"]["workspace_id"] = "another-workspace"
            if field == "official": changed["identity"]["official_account_id"] = "another-account"
            result = await self.refresh({"ok": True, "snapshot": changed})
            self.assertFalse(result["ok"])
            self.assertFalse(result["can_retry"])
            self.assertEqual(result["snapshot"], baseline["snapshot"])

    async def test_completed_steps_keep_pause_and_reject_older_receipt(self):
        done = fixture(state="completed")
        done["latest_operation"]["updated_at"] = "2026-09-11T12:01:00+00:00"
        result = await self.refresh({"ok": True, "snapshot": done})
        self.assertEqual(result["state"], "completed")
        self.assertIn("schedulable_off", result["snapshot"]["remaining_blockers"])
        self.assertFalse(result["can_retry"])
        old = await self.refresh({"ok": True, "snapshot": fixture()})
        self.assertEqual(old["snapshot"]["latest_operation"]["state"], "completed")
        self.assertEqual(old["error_code"], "older_observation")

    async def test_retry_rechecks_and_never_sends_credentials(self):
        retry = AsyncMock(return_value={"ok": True, "queued": True})
        source = AsyncMock(return_value={"ok": True, "snapshot": fixture()})
        with patch("app.application.sub2api_remote_state.request_state", new=source), patch("app.application.sub2api_remote_state.retry_followups", new=retry):
            result = await retry_remote_followups(self.session, self.account.id)
        self.assertTrue(result["ok"])
        self.assertEqual(source.await_count, 2)
        self.assertEqual(retry.await_args.args[2:], (42, "fixture-operation", INSTANCE))
        changed = fixture(); changed["instance_id"] = "b69d02db-8fb2-4f79-bef2-482476b1c100"
        retry.reset_mock()
        with patch("app.application.sub2api_remote_state.request_state", new=AsyncMock(return_value={"ok": True, "snapshot": changed})), patch("app.application.sub2api_remote_state.retry_followups", new=retry):
            result = await retry_remote_followups(self.session, self.account.id)
        self.assertFalse(result["ok"]); retry.assert_not_awaited()

    async def test_cached_state_is_read_only_and_becomes_stale(self):
        await self.refresh({"ok": True, "snapshot": fixture()})
        row = await self.session.get(Sub2ApiSyncObservation, self.binding.id)
        row.checked_at = datetime.now(timezone.utc) - timedelta(minutes=3)
        await self.session.commit()
        with patch("app.application.sub2api_remote_state.request_state", new=AsyncMock()) as remote:
            result = await cached_remote_state(self.session, self.account.id)
        remote.assert_not_awaited(); self.assertTrue(result["stale"]); self.assertFalse(result["can_retry"])

    async def test_unverified_binding_never_contacts_remote(self):
        self.binding.binding_state = "conflict"; await self.session.commit()
        with patch("app.application.sub2api_remote_state.request_state", new=AsyncMock()) as remote:
            result = await refresh_remote_state(self.session, self.account.id)
        self.assertFalse(result["ok"]); remote.assert_not_awaited()

    async def test_first_failure_is_persisted_and_later_success_pins(self):
        await self.refresh({"ok": False, "error_code": "bridge_unavailable"})
        async with self.session_maker() as fresh:
            state = await cached_remote_state(fresh, self.account.id)
            self.assertEqual(state["error_code"], "bridge_unavailable")
            self.assertIsNone(state["snapshot"])
        recovered = await self.refresh({"ok": True, "snapshot": fixture()})
        self.assertTrue(recovered["ok"]); self.assertEqual(recovered["snapshot"]["instance_id"], INSTANCE)

    async def test_slower_response_cannot_overwrite_concurrent_completed_observation(self):
        await self.refresh({"ok": True, "snapshot": fixture()})
        entered, release = asyncio.Event(), asyncio.Event()
        async with self.session_maker() as slow, self.session_maker() as fast:
            async def source(_client, db, _remote_id):
                if db is slow:
                    entered.set()
                    await release.wait()
                    return {"ok": True, "snapshot": fixture()}
                done = fixture(state="completed")
                done["latest_operation"]["updated_at"] = "2026-09-11T12:01:00+00:00"
                return {"ok": True, "snapshot": done}
            with patch("app.application.sub2api_remote_state.request_state", new=source):
                late = asyncio.create_task(refresh_remote_state(slow, self.account.id))
                await asyncio.wait_for(entered.wait(), 5)
                newer = await refresh_remote_state(fast, self.account.id)
                release.set()
                older = await asyncio.wait_for(late, 5)
            self.assertEqual(newer["state"], "completed")
            self.assertEqual(older["state"], "completed")
            self.assertEqual(older["observation_revision"], newer["observation_revision"])

    async def test_binding_changed_during_fetch_never_saves_old_target(self):
        async def source(*_args):
            self.binding.remote_account_id = "99"
            await self.session.commit()
            return {"ok": True, "snapshot": fixture()}
        with patch("app.application.sub2api_remote_state.request_state", new=source):
            result = await refresh_remote_state(self.session, self.account.id)
        self.assertEqual(result["error_code"], "binding_changed")
        self.assertIsNone(await self.session.get(Sub2ApiSyncObservation, self.binding.id))


class RemoteStateTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_state_parser_is_idempotent_and_never_keeps_extra_payloads(self):
        raw = fixture(); raw["raw"] = {"token": "fixture-secret"}
        clean = parse_state(raw, 42)
        self.assertEqual(parse_state(clean, 42), clean)
        self.assertNotIn("fixture-secret", json.dumps(clean))
        for changes in ({"credential_version": True}, {"remaining_blockers": "paused"}, {"remote_account_id": 43}):
            with self.assertRaises(ValueError): parse_state({**raw, **changes}, 42)

    async def test_http_only_reads_state_and_retry_carries_instance(self):
        requests = []
        def respond(request):
            requests.append(request)
            data = fixture() if request.method == "GET" else {"state": "recorded", "operation_id": "fixture-operation", "receipt": {"state": "pending", "remote_account_id": 42}}
            return httpx.Response(200, json={"code": 0, "data": data})
        client = Sub2ApiClient()
        async def opened(_):
            return httpx.AsyncClient(base_url="https://fixture.invalid", transport=httpx.MockTransport(respond)), {}, {}
        client._with_client = opened
        self.assertTrue((await request_state(client, None, 42))["ok"])
        self.assertTrue((await retry_followups(client, None, 42, "fixture-operation", INSTANCE))["ok"])
        self.assertEqual([r.method for r in requests], ["GET", "POST"])
        self.assertEqual(json.loads(requests[1].content), {"expected_instance_id": INSTANCE})
        self.assertNotIn("sync-oauth-credentials", requests[1].url.path)
