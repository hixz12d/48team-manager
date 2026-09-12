"""Persist partial/unknown receipts and read the state after explicit scheduling changes."""
import json
import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

import tests.test_sub2api_management as management
from app.application.sub2api_publish import account_sub2api_push, push_refreshed_tokens_to_bound_sub2api
from app.persistence.models.operations import Operation, OperationStep


class CredentialSyncPersistenceTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = management.Sub2ApiManagementTests.asyncSetUp
    asyncTearDown = management.Sub2ApiManagementTests.asyncTearDown

    def remote(self, schedulable=True):
        return {"id": 42, "platform": "openai", "type": "oauth", "status": "active", "schedulable": schedulable,
                "credentials": {"email": self.account.email, "chatgpt_account_id": self.account.official_account_id}}

    async def test_partial_and_unknown_survive_operation_persistence(self):
        for write, state in (("succeeded", "partial"), ("unknown", "unknown")):
            receipt = {"ok": False, "supported": True, "credential_write": write,
                       "auth_recovery": "skipped", "token_cache_invalidation": "failed",
                       "partial": True, "state": state, "error_code": "sync_oauth_incomplete"}
            final_read = AsyncMock()
            with (
                patch("app.application.sub2api_publish.sub2api_client.get_account", new=AsyncMock(return_value=self.remote())),
                patch("app.application.sub2api_publish.sub2api_client.read_after_write", new=final_read),
                patch("app.application.sub2api_publish.sync_bound_oauth_credentials", new=AsyncMock(return_value=receipt)) as sync,
                patch("app.application.sub2api_publish.decrypt_secret", return_value="fixture-token"),
            ):
                result = await push_refreshed_tokens_to_bound_sub2api(self.session, self.account, reason="manual_reauthorize")
            self.assertFalse(result["ok"])
            self.assertEqual(result["credential_write"], write)
            self.assertEqual(sync.await_args.kwargs["reason"], "manual_reauthorize")
            final_read.assert_not_awaited()
            await self.session.commit()
            async with self.session_maker() as fresh:
                op = (await fresh.execute(select(Operation).order_by(Operation.id.desc()).limit(1))).scalar_one()
                saved = json.loads(op.result_json)
                self.assertEqual(saved["credential_write"], write)
                self.assertEqual(saved["state"], state)
                self.assertFalse(saved["success"])
                step = (await fresh.execute(select(OperationStep).where(OperationStep.operation_id == op.id))).scalars().first()
                self.assertEqual(step.state, "partial")
                self.assertNotIn("fixture-token", op.result_json)

    async def test_manual_push_keeps_partial_receipt_and_does_not_write_config(self):
        receipt = {"ok": False, "supported": True, "credential_write": "succeeded", "partial": True,
                   "auth_recovery": "skipped", "token_cache_invalidation": "failed", "error_code": "sync_oauth_incomplete"}
        update = AsyncMock()
        with (
            patch("app.application.sub2api_publish.sync_bound_oauth_credentials", new=AsyncMock(return_value=receipt)),
            patch("app.application.sub2api_publish.sub2api_client.update_account", new=update),
            patch("app.application.sub2api_publish.decrypt_secret", return_value="fixture-token"),
        ):
            result = await account_sub2api_push(self.session, self.account.id)
        self.assertFalse(result["ok"])
        self.assertFalse(result["success"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["credential_write"], "succeeded")
        update.assert_not_awaited()

    async def test_pending_propagation_does_not_discard_explicit_metadata(self):
        receipt = {"ok": False, "supported": True, "credential_write": "succeeded", "partial": True,
                   "state": "pending", "auth_recovery": "skipped", "token_cache_invalidation": "pending",
                   "scheduler_refresh": "pending", "error_code": "sync_oauth_incomplete", "after": self.remote()}
        update = AsyncMock(return_value={"id": 42})
        with (
            patch("app.application.sub2api_publish.sync_bound_oauth_credentials", new=AsyncMock(return_value=receipt)),
            patch("app.application.sub2api_publish.sub2api_client.update_account", new=update),
            patch("app.application.sub2api_publish.sub2api_client.read_after_write", new=AsyncMock(return_value=self.remote())),
            patch("app.application.sub2api_publish.decrypt_secret", return_value="fixture-token"),
        ):
            result = await account_sub2api_push(self.session, self.account.id, name="explicit-name")
        self.assertEqual(update.await_args.args[2], {"name": "explicit-name"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "pending")
        self.assertEqual(result["credential_write"], "succeeded")
        self.assertEqual(result["scheduler_refresh"], "pending")

    async def test_metadata_failure_preserves_committed_credential_receipt(self):
        receipt = {"ok": False, "supported": True, "credential_write": "succeeded", "partial": True,
                   "state": "pending", "auth_recovery": "skipped", "token_cache_invalidation": "pending",
                   "scheduler_refresh": "pending", "error_code": "sync_oauth_incomplete", "after": self.remote()}
        with (
            patch("app.application.sub2api_publish.sync_bound_oauth_credentials", new=AsyncMock(return_value=receipt)),
            patch("app.application.sub2api_publish.sub2api_client.update_account", new=AsyncMock(side_effect=RuntimeError("fixture failure"))),
            patch("app.application.sub2api_publish.decrypt_secret", return_value="fixture-token"),
        ):
            result = await account_sub2api_push(self.session, self.account.id, name="explicit-name")
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["error_code"], "push_followup_failed")
        self.assertEqual(result["credential_write"], "succeeded")

    async def test_explicit_pause_is_followed_by_final_read(self):
        paused = False
        calls = []

        async def set_pause(*args):
            nonlocal paused
            paused = True
            calls.append("pause")
            return {"patched": True}

        async def read(*args):
            calls.append("read")
            return self.remote(not paused)

        with (
            patch("app.application.sub2api_publish.sub2api_client.get_account", new=AsyncMock(return_value=self.remote())),
            patch("app.application.sub2api_publish.sub2api_client.read_after_write", new=AsyncMock(side_effect=read)),
            patch("app.application.sub2api_publish.sub2api_client.set_account_schedulable", new=AsyncMock(side_effect=set_pause)),
            patch("app.application.sub2api_publish.sub2api_client.sync_oauth_credentials", new=AsyncMock(return_value={
                "ok": True, "supported": True, "credential_write": "succeeded", "auth_recovery": "skipped",
                "token_cache_invalidation": "succeeded", "partial": False,
            })),
            patch("app.application.sub2api_publish.decrypt_secret", return_value="fixture-token"),
        ):
            result = await account_sub2api_push(self.session, self.account.id, schedulable=False)
        self.assertEqual(calls[-2:], ["pause", "read"])
        self.assertIn("schedulable_off", result["remaining_blockers"])
        self.assertFalse(result["ok"])
