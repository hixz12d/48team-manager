import asyncio
import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application import sub2api_status as status
from app.application.queries.portfolio import portfolio_query
from app.application.quota import QuotaService
from app.core.time import utcnow
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding
from app.persistence.models.sub2api_status import Sub2ApiAccountStatus


class RemoteAccountStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.account = Account(email="child@example.com", local_purpose="child", auth_state="healthy",
                               access_token_encrypted="sealed-AT", refresh_token_encrypted="sealed-RT")
        self.db.add(self.account)
        await self.db.flush()
        self.binding = ExternalBinding(provider="sub2api", local_account_id=self.account.id,
                                       remote_account_id="42", binding_state="verified", verified_email=self.account.email)
        self.db.add(self.binding)
        await self.db.commit()
        self.remote = {"id": 42, "status": "active", "schedulable": True,
                       "credentials": {"email": self.account.email, "access_token": "SECRET-AT", "refresh_token": "SECRET-RT"}}
        self.config = {"configured": True, "base_url": "http://sub.invalid", "api_key": "SECRET-KEY"}
        self.config_mock = self.enterContext(patch.object(status.sub2api_client, "load_config", new=AsyncMock(return_value=self.config)))
        self.list_mock = self.enterContext(patch.object(status.sub2api_client, "list_status_accounts", new=AsyncMock(return_value=[self.remote])))
        self.enterContext(patch.object(status, "_LOCK", asyncio.Lock()))

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def current(self):
        values, _ = await status.payloads(self.db)
        return values[(self.account.id, None)]

    async def test_remote_deletion_is_visible_without_changing_local_account_or_binding(self):
        await status.refresh(self.db)
        self.assertEqual((await self.current())["state"], "healthy")
        self.list_mock.return_value = []
        await status.refresh(self.db, force=True)
        current = await self.current()
        self.assertEqual(current["state"], "missing")
        self.assertFalse(current["exists"])
        self.assertFalse(current["stale"])
        self.assertEqual(self.account.auth_state, "healthy")
        self.assertEqual(self.binding.binding_state, "verified")
        self.assertEqual(self.account.access_token_encrypted, "sealed-AT")
        self.assertIsNotNone(await self.db.get(Account, self.account.id))
        portfolio = await portfolio_query(self.db)
        self.assertEqual(portfolio["accounts"][0]["remote_status"]["state"], "missing")
        self.assertEqual(portfolio["sub2api_status"]["missing"], 1)

    async def test_remote_pause_and_recovery_are_read_back(self):
        for remote, expected in [({"status": "inactive", "schedulable": False}, "paused"),
                                 ({"status": "error", "error_message": "401 revoked SECRET"}, "auth_error"),
                                 ({"status": "active", "schedulable": True}, "healthy")]:
            self.list_mock.return_value = [{**self.remote, **remote}]
            await status.refresh(self.db, force=True)
            self.assertEqual((await self.current())["state"], expected)

    async def test_old_quota_and_error_text_do_not_override_active_remote_state(self):
        self.list_mock.return_value = [{**self.remote, "error_message": "old 401 error",
                                       "extra": {"codex_7d_used_percent": 100}}]
        await status.refresh(self.db)
        self.assertEqual((await self.current())["state"], "healthy")

    async def test_network_auth_404_and_malformed_failures_never_mean_deleted(self):
        await status.refresh(self.db)
        original = (await self.current())["checked_at"]
        failures = [httpx.ConnectError("SECRET NETWORK"), TimeoutError(), ValueError("SECRET PARSE")]
        for code in (401, 403, 404, 500):
            response = httpx.Response(code, request=httpx.Request("GET", "http://sub.invalid/api/v1/admin/accounts"))
            failures.append(httpx.HTTPStatusError("SECRET HTTP", request=response.request, response=response))
        for failure in failures:
            with self.subTest(error=type(failure).__name__):
                self.list_mock.side_effect = failure
                result = await status.refresh(self.db, force=True)
                self.assertFalse(result["ok"])
                current = await self.current()
                self.assertEqual(current["state"], "unknown")
                self.assertIsNone(current["exists"])
                self.assertEqual(current["last_known_state"], "healthy")
                self.assertEqual(current["checked_at"], original)
                self.assertTrue(current["stale"])
                self.assertNotIn("SECRET", json.dumps(current))
        self.list_mock.side_effect = None
        await status.refresh(self.db, force=True)
        self.assertEqual((await self.current())["state"], "healthy")

    async def test_invalid_list_does_not_mark_missing(self):
        for invalid in ({"oops": []}, [{"id": None}], [self.remote, self.remote], [None]):
            with self.subTest(data=invalid):
                self.list_mock.return_value = invalid
                self.assertFalse((await status.refresh(self.db, force=True))["ok"])
                self.assertIsNone((await self.current())["exists"])

    async def test_status_reads_are_local_and_do_not_reveal_credentials(self):
        await status.refresh(self.db)
        self.list_mock.reset_mock()
        payload = await portfolio_query(self.db)
        self.list_mock.assert_not_called()
        self.assertNotIn("SECRET", json.dumps(payload))
        row = await self.db.get(Sub2ApiAccountStatus, self.binding.id)
        self.assertNotIn("SECRET", repr(row.__dict__))

    async def test_throttle_and_force_refresh(self):
        await status.refresh(self.db)
        self.assertTrue((await status.refresh(self.db))["cached"])
        self.assertEqual(self.list_mock.await_count, 1)
        await status.refresh(self.db, force=True)
        self.assertEqual(self.list_mock.await_count, 2)

    async def test_overlapping_refreshes_are_coalesced(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def slow(*args):
            started.set()
            await release.wait()
            return [self.remote]
        self.list_mock.side_effect = slow
        pending = asyncio.create_task(status.refresh(self.db))
        await started.wait()
        try:
            self.assertTrue((await status.refresh(self.db, force=True))["refreshing"])
        finally:
            release.set()
            await pending
        self.assertEqual(self.list_mock.await_count, 1)

    async def test_stale_and_changed_connection_do_not_claim_fresh_status(self):
        await status.refresh(self.db)
        row = await self.db.get(Sub2ApiAccountStatus, self.binding.id)
        row.checked_at = utcnow() - timedelta(minutes=2)
        await self.db.commit()
        self.assertTrue((await self.current())["stale"])
        self.config_mock.return_value = {**self.config, "base_url": "http://other.invalid"}
        self.assertIsNone((await self.current())["exists"])
        self.assertEqual((await self.current())["state"], "unknown")

    async def test_changed_binding_is_not_assigned_old_result(self):
        async def rebind(*args):
            self.binding.remote_account_id = "43"
            await self.db.commit()
            return []
        self.list_mock.side_effect = rebind
        self.assertEqual((await status.refresh(self.db))["checked"], 0)
        self.assertIsNone((await self.current())["exists"])

    async def test_matching_id_with_wrong_email_is_not_healthy(self):
        self.remote["credentials"]["email"] = "someone-else@example.com"
        await status.refresh(self.db)
        self.assertEqual((await self.current())["state"], "identity_mismatch")
        self.assertEqual(self.account.email, "child@example.com")

    async def test_no_configuration_makes_no_remote_requests(self):
        self.config_mock.return_value = {"configured": False}
        result = await status.refresh(self.db)
        self.assertEqual(result["error_code"], "not_configured")
        self.list_mock.assert_not_called()

    async def test_remote_owned_refresh_token_is_not_reported_as_missing_oauth(self):
        from app.persistence.models.sub2api import Sub2ApiRefreshAuthority
        self.account.refresh_token_encrypted = None
        self.db.add(Sub2ApiRefreshAuthority(account_id=self.account.id, binding_id=self.binding.id,
            binding_fingerprint="binding", instance_id="instance", remote_account_id="42", remote_version=1,
            local_revision=1, local_fingerprint="local", epoch=1, last_success_at=utcnow(), last_attempt_at=utcnow()))
        await self.db.commit()
        health = (await QuotaService().health_reader(self.db))(self.account, None)
        self.assertFalse(health["needs_auth"])
        self.assertIn("已授权", health["health"]["label"])


class InventoryTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_success_response_is_not_an_empty_inventory(self):
        from app.integrations.sub2api.client import Sub2ApiClient
        client = Sub2ApiClient()
        for payload in ({}, {"items": "invalid"}, {"items": [None]}, {"items": [], "total": 1}):
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0, "data": payload}))
            async with httpx.AsyncClient(base_url="http://sub.invalid", transport=transport) as http:
                with self.assertRaises(RuntimeError):
                    await client._paginate_admin(http, {}, "/api/v1/admin/accounts")

    async def test_complete_empty_inventory_is_valid(self):
        from app.integrations.sub2api.client import Sub2ApiClient
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 0, "data": {"items": [], "total": 0}}))
        async with httpx.AsyncClient(base_url="http://sub.invalid", transport=transport) as http:
            self.assertEqual(await Sub2ApiClient()._paginate_admin(http, {}, "/api/v1/admin/accounts"), [])


if __name__ == "__main__":
    unittest.main()
