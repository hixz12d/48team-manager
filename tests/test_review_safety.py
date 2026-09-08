"""Offline regressions for the c6647fe review safety boundaries."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx

from app.application.rotate import RotateService
from app.application.sub2api_credential_sync import assess_auth_error, sync_bound_oauth_credentials
from app.integrations.openai.chatgpt import ChatGPTClient
from app.integrations.openai.member_adapter import classify_invite_submit_error
from app.integrations.sub2api.client import Sub2ApiClient


def remote(**overrides):
    return {"id": 55, "email": "kid@example.com", "workspace_id": "ws-a",
            "platform": "openai", "type": "oauth", "status": "active",
            "schedulable": True, **overrides}


class ReviewSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def sync(self, client, **kwargs):
        with patch("app.application.sub2api_credential_sync.sub2api_client", client):
            return await sync_bound_oauth_credentials(
                AsyncMock(), remote_id=55, credentials={"access_token": "synthetic"},
                expected_email="kid@example.com", expected_workspace_id="ws-a", **kwargs,
            )

    async def test_identity_unknown_or_conflict_never_writes(self):
        for before in (remote(workspace_id="ws-b"), remote(email=""), remote(workspace_id=None),
                       remote(id=56), remote(platform="anthropic"), {}, remote(type="apikey")):
            with self.subTest(before=before):
                client = AsyncMock()
                client.get_account.return_value = before
                result = await self.sync(client, prefetched_remote=before)
                self.assertFalse(result["ok"])
                client.sync_oauth_credentials.assert_not_awaited()
                client.update_account.assert_not_awaited()
                client.create_account.assert_not_awaited()

    async def test_fallback_write_failure_never_claims_synced(self):
        client = AsyncMock()
        client.get_account.return_value = remote()
        client.sync_oauth_credentials.return_value = {"ok": False, "supported": False}
        client.update_account.side_effect = RuntimeError("synthetic failure")
        result = await self.sync(client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["credential_write"], "failed")
        self.assertNotIn("已同步", result["message"])
        self.assertNotIn("已按兼容路径同步", result["message"])

    async def test_final_error_is_a_current_blocker(self):
        client = AsyncMock()
        client.get_account.return_value = remote()
        client.sync_oauth_credentials.return_value = {
            "ok": True, "supported": True, "credential_write": "succeeded",
            "auth_recovery": "cleared", "token_cache_invalidation": "succeeded",
        }
        client.read_after_write.return_value = remote(status="error", error_message="new 401")
        result = await self.sync(client)
        self.assertTrue(result["partial"])
        self.assertIn("final_status_blocked", result["remaining_blockers"])
        self.assertEqual(result["auth_recovery"], "cleared")
        self.assertEqual(result["final_error"], "new 401")
        self.assertEqual(client.sync_oauth_credentials.await_args.kwargs["recovery_mode"], "credentials_only")

    def test_protocol_words_are_not_auth_evidence(self):
        for message in ("OAuth upstream returned 429: usage_limit_reached", "OAuth 403 permission denied",
                        "invoice 401 failed", "access token quota limit", "authentication unavailable"):
            self.assertFalse(assess_auth_error(remote(status="error", error_message=message)))

    async def test_pause_requires_post_read(self):
        client = AsyncMock()
        client.set_account_schedulable.return_value = {"patched": True, "schedulable_verified": False}
        for after in ({}, {"id": 55, "schedulable": True}, {"id": 56, "schedulable": False}):
            client.get_account.return_value = after
            service = RotateService(sub2api=client)
            result = await service._pause_and_drain(AsyncMock(), job_id=None, remote_id=55, drain_seconds=0)
            self.assertFalse(result["ok"])

    async def test_revoke_stale_invite_gets_another_read(self):
        workspaces = AsyncMock()
        workspaces.lookup_live_member.side_effect = [
            ({"success": True}, {"status": "invited"}), ({"success": True}, None),
        ]
        result = await RotateService(workspaces=workspaces)._confirm_member_absent(
            AsyncMock(), workspace=SimpleNamespace(id=1), email="kid@example.com",
            expected_action="revoke", in_test=True,
        )
        self.assertTrue(result["confirmed"])
        self.assertEqual(workspaces.lookup_live_member.await_count, 2)

    async def test_revoke_acceptance_does_not_become_kick(self):
        workspaces = AsyncMock()
        workspaces.lookup_live_member.return_value = ({"success": True}, {"status": "joined"})
        result = await RotateService(workspaces=workspaces)._confirm_member_absent(
            AsyncMock(), workspace=SimpleNamespace(id=1), email="kid@example.com",
            expected_action="revoke", in_test=True,
        )
        self.assertEqual(result["state"], "state_changed")
        workspaces.delete_member.assert_not_awaited()

    async def test_invite_timeout_posts_once_then_reads(self):
        client = ChatGPTClient()
        session = AsyncMock()
        session.post.side_effect = TimeoutError("synthetic timeout")
        session.get.return_value = SimpleNamespace(status_code=200, json=lambda: {"items": [], "total": 0})
        with patch.object(client, "_get_session", AsyncMock(return_value=session)):
            result = await client.send_invite("synthetic", "ws-a", "kid@example.com", None)
        self.assertEqual(session.post.await_count, 1)
        self.assertEqual(session.get.await_count, 2)
        self.assertFalse(result["retryable"])
        self.assertTrue(result["outcome_unknown"])

    async def test_write_5xx_is_not_retried(self):
        client = ChatGPTClient()
        session = AsyncMock()
        session.post.return_value = SimpleNamespace(status_code=503, text="unavailable", json=lambda: {}, headers={})
        with patch.object(client, "_get_session", AsyncMock(return_value=session)):
            result = await client._make_request("POST", "https://example.invalid", {})
        self.assertEqual(session.post.await_count, 1)
        self.assertTrue(result["outcome_unknown"])

    async def test_sync_contract_rejects_business_failure_or_wrong_target(self):
        for fields in ({"credential_write": "failed"}, {"token_cache_invalidation": "failed"},
                       {"remote_account_id": 56}, {"operation_id": "wrong"}, {"auth_recovery": None}, {"success": False}):
            data = {"contract_version": 1, "remote_account_id": 55, "operation_id": "op",
                    "credential_write": "succeeded", "token_cache_invalidation": "succeeded",
                    "auth_recovery": "skipped", **fields}
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0, "data": data}))
            client = Sub2ApiClient()
            http = httpx.AsyncClient(transport=transport, base_url="https://example.invalid")
            with patch.object(client, "_with_client", AsyncMock(return_value=(http, {}, {}))):
                result = await client.sync_oauth_credentials(AsyncMock(), 55, credentials={}, operation_id="op")
            self.assertFalse(result["ok"])

    def test_nested_email_validation_is_not_seat_error(self):
        self.assertIsNone(classify_invite_submit_error(status_code=422, error_body={
            "detail": [{"loc": ["body", "email_addresses"], "msg": "invalid email"}],
        }))
        result = classify_invite_submit_error(status_code=422, error_body={
            "detail": [{"loc": ["body", "seat_type"], "msg": "invalid enum"}],
        })
        self.assertEqual(result["field"], "seat_type")

    async def test_compatibility_final_get_failure_preserves_write_fact(self):
        client = AsyncMock()
        client.get_account.return_value = remote()
        client.sync_oauth_credentials.return_value = {"ok": False, "supported": False}
        client.read_after_write.side_effect = TimeoutError("read timeout")
        result = await self.sync(client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["credential_write"], "succeeded")
        self.assertEqual(result["error_code"], "final_get_failed")

    async def test_delete_200_business_failure_and_mixed_receipt(self):
        for ids, body, deleted in (
            ([55], {"success": False}, []),
            ([55], {"error": "failed"}, []),
            ([55], {"code": 0, "data": None}, [55]),
            ([55, 56], {"code": 0, "data": {"deleted": [55], "failed": [{"id": 56}]}}, [55]),
            ([55, 56], {"code": 0, "data": {"message": "accepted"}}, []),
            ([55, 56], {"code": 0, "data": {"success": False, "deleted": [55, 56]}}, []),
        ):
            client = Sub2ApiClient()
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=body))
            http = httpx.AsyncClient(transport=transport, base_url="https://example.invalid")
            with patch.object(client, "_with_client", AsyncMock(return_value=(http, {}, {}))):
                result = await client.delete_accounts(AsyncMock(), ids)
            self.assertEqual(result["deleted"], deleted)
            self.assertEqual({item["id"] for item in result["failed"]}, set(ids) - set(deleted))

    async def test_workspace_lock_serializes_independent_sessions(self):
        import asyncio
        import tempfile
        from pathlib import Path
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
        from app.application.operations import operation_store
        from app.persistence.database import Base

        with tempfile.TemporaryDirectory() as folder:
            engine = create_async_engine("sqlite+aiosqlite:///" + (Path(folder) / "locks.db").as_posix())
            try:
                async with engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                factory = async_sessionmaker(engine, expire_on_commit=False)
                gate = asyncio.Event()

                async def acquire(workspace_id, op_type):
                    async with factory() as session:
                        await gate.wait()
                        operation, blocker = await operation_store.create_workspace_locked(
                            session, workspace_id=workspace_id, op_type=op_type,
                        )
                        await session.commit()
                        return operation is not None, blocker is not None

                tasks = [asyncio.create_task(acquire(1, action)) for action in ("kick_member", "replenish")]
                gate.set()
                results = await asyncio.gather(*tasks)
                self.assertEqual(sum(success for success, _ in results), 1)
                self.assertEqual(sum(blocked for _, blocked in results), 1)
                results = await asyncio.gather(acquire(2, "invite_child"), acquire(3, "update_member_role"))
                self.assertEqual(results, [(True, False), (True, False)])
            finally:
                await engine.dispose()

    def test_seat_settings_do_not_leak_between_requests(self):
        from app.integrations.openai.member_adapter import apply_verified_seat_wire_settings, InviteSeatIntent
        customized = apply_verified_seat_wire_settings({"premium": "custom"})
        defaults = apply_verified_seat_wire_settings({"premium": ""})
        self.assertEqual(customized[InviteSeatIntent.PREMIUM], "custom")
        self.assertEqual(defaults[InviteSeatIntent.PREMIUM], "prolite")
