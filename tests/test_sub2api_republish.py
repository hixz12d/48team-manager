"""Manual republish after a confirmed remote deletion, including retry behavior."""
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.sub2api_publish import account_sub2api_push, account_sub2api_reconcile
from app.core.time import utcnow
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding
from app.persistence.models.sub2api import Sub2ApiSyncObservation, Sub2ApiUsageSnapshot


def http_error(status):
    response = httpx.Response(status, request=httpx.Request("GET", "https://sub2api.example/api/v1/admin/accounts/42"))
    return httpx.HTTPStatusError("remote read failed", request=response.request, response=response)


class Sub2ApiRepublishTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.account = Account(email="republish@example.com", operational_state="active", local_purpose="child",
                               access_token_encrypted="access", refresh_token_encrypted="refresh")
        self.db.add(self.account)
        await self.db.flush()
        self.binding = ExternalBinding(provider="sub2api", local_account_id=self.account.id,
                                       remote_account_id="42", binding_state="missing")
        self.db.add(self.binding)
        await self.db.commit()
        self.remote = {"id": 43, "platform": "openai", "type": "oauth", "status": "active",
                       "schedulable": True, "credentials": {"email": self.account.email},
                       "extra": {"team48_context_key": f"team48::{self.account.id}"}}
        self.get = AsyncMock(side_effect=http_error(404))
        self.list = AsyncMock(return_value=[])
        self.create = AsyncMock(return_value={"id": 43})
        self.readback = AsyncMock(return_value=self.remote)
        self.sync = AsyncMock(return_value={"ok": True, "credential_write": "succeeded", "after": self.remote})
        for name, mock in (("get_account", self.get), ("list_status_accounts", self.list),
                           ("create_account", self.create), ("read_after_write", self.readback)):
            p = patch(f"app.application.sub2api_publish.sub2api_client.{name}", new=mock)
            p.start()
            self.addCleanup(p.stop)
        for target, kwargs in (
            ("decrypt_secret", {"side_effect": lambda v: v or ""}),
            ("sync_bound_oauth_credentials", {"new": self.sync}),
        ):
            p = patch(f"app.application.sub2api_publish.{target}", **kwargs)
            p.start()
            self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def push(self):
        return await account_sub2api_push(self.db, self.account.id)

    async def test_missing_binding_republishes_clears_old_caches_and_next_push_updates(self):
        self.db.add(Sub2ApiSyncObservation(binding_id=self.binding.id, instance_id="old-instance",
                    remote_account_id="42", snapshot_json="{}", checked_at=utcnow()))
        self.db.add(Sub2ApiUsageSnapshot(binding_id=self.binding.id, local_account_id=self.account.id,
                    remote_account_id="42", window_kind="today", total_tokens=100))
        await self.db.commit()
        result = await self.push()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.binding.remote_account_id, "43")
        self.assertEqual(self.binding.binding_state, "verified")
        self.assertIsNone(await self.db.get(Sub2ApiSyncObservation, self.binding.id))
        self.assertIsNone(await self.db.scalar(select(Sub2ApiUsageSnapshot)))
        self.get.side_effect = None
        self.get.return_value = self.remote
        again = await self.push()
        self.assertTrue(again["ok"], again)
        self.assertEqual(again["action"], "update")
        self.create.assert_awaited_once()

    async def test_reconcile_explains_republish_without_creating(self):
        result = await account_sub2api_reconcile(self.db, self.account.id)
        self.assertEqual(result["outcome"], "binding_remote_missing")
        self.assertIn("点击推送重新创建", result["message"])
        self.create.assert_not_awaited()

    async def test_non_404_read_errors_preserve_binding(self):
        for status in (401, 403, 500):
            with self.subTest(status=status):
                self.get.side_effect = http_error(status)
                self.assertFalse((await self.push())["ok"])
                self.assertEqual(self.binding.remote_account_id, "42")
        self.get.side_effect = httpx.ReadTimeout("timeout")
        self.assertFalse((await self.push())["ok"])
        self.create.assert_not_awaited()

    async def test_detail_404_with_existing_list_entry_does_not_create(self):
        self.list.return_value = [{**self.remote, "id": 42}]
        self.assertFalse((await self.push())["ok"])
        self.create.assert_not_awaited()
        self.assertEqual(self.binding.remote_account_id, "42")

    async def test_failed_list_or_create_preserves_old_binding(self):
        self.list.side_effect = RuntimeError("incomplete list")
        self.assertFalse((await self.push())["ok"])
        self.create.assert_not_awaited()
        self.list.side_effect = None
        self.create.side_effect = RuntimeError("write failed")
        self.assertFalse((await self.push())["ok"])
        self.assertEqual(self.binding.remote_account_id, "42")

    async def test_existing_context_target_is_reused(self):
        self.list.return_value = [self.remote]
        result = await self.push()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.binding.remote_account_id, "43")
        self.create.assert_not_awaited()
        self.sync.assert_awaited_once()

    async def test_ambiguous_context_does_not_create(self):
        self.list.return_value = [self.remote, {**self.remote, "id": 44}]
        self.assertFalse((await self.push())["ok"])
        self.create.assert_not_awaited()
        self.assertEqual(self.binding.remote_account_id, "42")

    async def test_conflict_binding_is_not_recreated(self):
        self.binding.binding_state = "conflict"
        await self.db.commit()
        self.assertFalse((await self.push())["ok"])
        self.create.assert_not_awaited()

    async def test_remote_owned_account_with_fresh_oauth_can_republish(self):
        from app.application.sub2api_refresh_authority import local_credential_fingerprint
        from app.persistence.models.sub2api import Sub2ApiRefreshAuthority
        owner = Sub2ApiRefreshAuthority(account_id=self.account.id, binding_id=self.binding.id,
            binding_fingerprint="old", instance_id="old-instance", remote_account_id="42",
            remote_version=1, local_revision=self.account.credential_revision,
            local_fingerprint=local_credential_fingerprint(self.account), epoch=1,
            last_success_at=utcnow(), last_attempt_at=utcnow())
        self.db.add(owner)
        self.account.credential_revision += 1
        self.account.refresh_token_encrypted = "new-oauth-refresh"
        await self.db.commit()
        result = await self.push()
        self.assertTrue(result["ok"], result)
        self.assertIsNone(await self.db.get(Sub2ApiRefreshAuthority, self.account.id))
        self.assertEqual(self.binding.remote_account_id, "43")
        self.assertEqual(self.create.await_args.args[1]["credentials"]["refresh_token"], "new-oauth-refresh")

    async def test_matched_target_bound_elsewhere_is_not_modified(self):
        other = Account(email="other@example.com", local_purpose="child")
        self.db.add(other)
        await self.db.flush()
        self.db.add(ExternalBinding(provider="sub2api", local_account_id=other.id,
                                   remote_account_id="43", binding_state="verified"))
        await self.db.commit()
        self.list.return_value = [self.remote]
        self.assertFalse((await self.push())["ok"])
        self.sync.assert_not_awaited()
        self.create.assert_not_awaited()
        self.assertEqual(self.binding.remote_account_id, "42")

    async def test_restored_remote_is_updated_after_missing_state(self):
        self.remote["id"] = 42
        self.get.side_effect = None
        self.get.return_value = self.remote
        result = await self.push()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.binding.binding_state, "verified")
        self.create.assert_not_awaited()

    async def test_readback_failure_keeps_new_id_and_retry_updates_it(self):
        self.readback.side_effect = httpx.ReadTimeout("readback unavailable")
        result = await self.push()
        self.assertFalse(result["ok"])
        self.assertEqual(self.binding.remote_account_id, "43")
        self.assertEqual(self.binding.binding_state, "pending")
        self.readback.side_effect = None
        self.get.side_effect = None
        self.get.return_value = self.remote
        again = await self.push()
        self.assertTrue(again["ok"], again)
        self.create.assert_awaited_once()
        self.assertEqual(self.binding.binding_state, "verified")
