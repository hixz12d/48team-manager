"""Sub2API creation defaults use native proxy pools without altering bound accounts."""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

import httpx
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.settings import get_setting_value
from app.application.sub2api_defaults import SETTING_KEY, load_defaults, push_options, save_defaults
from app.application.sub2api_publish import account_sub2api_push
from app.integrations.sub2api.client import Sub2ApiClient
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding
from app.web.schemas.settings import Sub2ApiPushDefaults
from tests.helpers import make_client


class DefaultsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.account = Account(email="child@example.com", local_purpose="child", operational_state="active",
                               access_token_encrypted="access", refresh_token_encrypted="refresh")
        self.db.add(self.account)
        await self.db.commit()
        self.remote = {"id": 43, "platform": "openai", "type": "oauth", "status": "active", "schedulable": True,
                       "credentials": {"email": self.account.email}, "proxy_id": 101, "concurrency": 5, "group_ids": [7]}
        self.proxy_groups = [{"id": 9, "name": "IPv6 美国", "max_accounts_per_proxy": 2,
                              "proxy_ids": [101, 102], "available_proxy_ids": [101, 102]}]
        self.groups = [{"id": 7, "name": "OpenAI 默认分组", "platform": "openai", "status": "active"}]
        self.mocks = {}
        for name, result in (("list_groups", self.groups), ("list_proxy_groups", self.proxy_groups),
                             ("list_proxies", [{"id": 101, "name": "固定出口", "status": "active", "password": "SECRET"}]),
                             ("load_config", {"configured": True}), ("list_status_accounts", []),
                             ("create_account", self.remote), ("get_account", self.remote),
                             ("read_after_write", self.remote), ("update_account", self.remote)):
            mock = AsyncMock(return_value=result)
            self.mocks[name] = mock
            p = patch(f"app.integrations.sub2api.client.sub2api_client.{name}", new=mock)
            p.start(); self.addCleanup(p.stop)
        p = patch("app.application.sub2api_publish.decrypt_secret", side_effect=lambda value: value or "")
        p.start(); self.addCleanup(p.stop)

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def store(self, **values):
        await save_defaults(self.db, Sub2ApiPushDefaults(**values))
        await self.db.commit()

    async def test_default_is_five_and_saved_selections_round_trip(self):
        self.assertEqual((await load_defaults(self.db)).concurrency, 5)
        await self.store(concurrency=8, group_ids=[7, 7], proxy_group_id=9)
        self.assertEqual((await load_defaults(self.db)).model_dump(),
                         {"concurrency": 8, "group_ids": [7], "proxy_group_id": 9, "proxy_id": None})

    async def test_create_sends_pool_not_a_locally_chosen_proxy(self):
        await self.store(group_ids=[7], proxy_group_id=9)
        preview = await account_sub2api_push(self.db, self.account.id, dry_run=True)
        self.assertEqual(preview["creation_defaults"], {"concurrency": 5, "group_ids": [7], "proxy_group_id": 9})
        self.mocks["create_account"].assert_not_awaited()
        result = await account_sub2api_push(self.db, self.account.id)
        self.assertTrue(result["ok"], result)
        body = self.mocks["create_account"].await_args.args[1]
        self.assertEqual(body["concurrency"], 5)
        self.assertEqual(body["group_ids"], [7])
        self.assertEqual(body["proxy_group_id"], 9)
        self.assertNotIn("proxy_id", body)
        self.assertEqual(result["proxy_id"], 101)

    async def test_explicit_group_selection_overrides_saved_defaults(self):
        await self.store(group_ids=[7], proxy_id=101, concurrency=8)
        result = await account_sub2api_push(self.db, self.account.id, group_ids=[])
        self.assertTrue(result["ok"], result)
        body = self.mocks["create_account"].await_args.args[1]
        self.assertEqual(body["group_ids"], [])
        self.assertEqual(body["proxy_id"], 101)
        self.assertEqual(body["concurrency"], 8)
        self.assertNotIn("proxy_group_id", body)

    async def test_bound_update_ignores_creation_defaults_even_when_catalog_is_offline(self):
        await self.store(group_ids=[7], proxy_group_id=9)
        self.db.add(ExternalBinding(provider="sub2api", local_account_id=self.account.id,
                                   remote_account_id="43", binding_state="verified"))
        await self.db.commit()
        self.mocks["list_proxy_groups"].side_effect = RuntimeError("offline")
        sync = AsyncMock(return_value={"ok": True, "credential_write": "succeeded", "after": self.remote})
        with patch("app.application.sub2api_publish.sync_bound_oauth_credentials", new=sync):
            result = await account_sub2api_push(self.db, self.account.id)
        self.assertTrue(result["ok"], result)
        self.mocks["create_account"].assert_not_awaited()
        self.mocks["update_account"].assert_not_awaited()
        self.assertIn("proxy_id", result["preserved_fields"])
        self.assertIn("concurrency", result["preserved_fields"])
        self.assertIn("group_ids", result["preserved_fields"])

    async def test_full_pool_fails_without_retry_or_direct_connection_fallback(self):
        await self.store(proxy_group_id=9)
        response = httpx.Response(409, json={"code": 409, "reason": "PROXY_GROUP_FULL"}, request=httpx.Request("POST", "http://fixture.invalid/accounts"))
        self.mocks["create_account"].side_effect = httpx.HTTPStatusError("full", request=response.request, response=response)
        result = await account_sub2api_push(self.db, self.account.id)
        self.assertFalse(result["ok"])
        self.assertIn("容量", result["message"])
        self.mocks["create_account"].assert_awaited_once()
        self.mocks["update_account"].assert_not_awaited()

    async def test_missing_pool_or_unsupported_endpoint_prevents_creation(self):
        await self.store(proxy_group_id=9)
        for error in (None, RuntimeError("unsupported")):
            self.mocks["list_proxy_groups"].return_value = []
            self.mocks["list_proxy_groups"].side_effect = error
            result = await account_sub2api_push(self.db, self.account.id)
            self.assertFalse(result["ok"])
        self.mocks["create_account"].assert_not_awaited()

    async def test_unconfirmed_assignment_is_partial_and_keeps_remote_binding(self):
        await self.store(proxy_group_id=9)
        self.remote["proxy_id"] = None
        result = await account_sub2api_push(self.db, self.account.id)
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["error_code"], "configuration_unconfirmed")
        self.assertEqual(result["remote_id"], 43)

    async def test_options_are_filtered_and_never_expose_proxy_secrets(self):
        self.groups.extend([{"id": 8, "name": "Anthropic", "platform": "anthropic", "status": "active"},
                            {"id": 10, "name": "Disabled", "platform": "openai", "status": "inactive"}])
        options = await push_options(self.db)
        self.assertEqual([row["id"] for row in options["groups"]], [7])
        self.assertEqual(options["proxy_groups"][0]["available_proxy_count"], 2)
        self.assertNotIn("SECRET", json.dumps(options))
        self.assertNotIn("password", json.dumps(options))
        self.mocks["list_proxy_groups"].side_effect = RuntimeError("secret error")
        options = await push_options(self.db)
        self.assertIn("proxy_groups", options["errors"])
        self.assertNotIn("secret error", json.dumps(options))
        self.assertEqual(len(options["groups"]), 1)

    async def test_unavailable_defaults_are_not_persisted(self):
        with self.assertRaises(ValueError):
            await self.store(group_ids=[99])
        self.assertIsNone(await get_setting_value(self.db, SETTING_KEY))


class DefaultsAPITests(unittest.TestCase):
    def test_schema_validation_and_settings_persistence(self):
        with TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            self.assertEqual(client.get("/api/sub2api/push-options").status_code, 401)
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            self.assertEqual(client.get("/api/settings").json()["sub2api_push"]["concurrency"], 5)
            for invalid in ({"concurrency": 0}, {"concurrency": 1001}, {"group_ids": [-1]}, {"proxy_id": 1, "proxy_group_id": 2}):
                self.assertEqual(client.patch("/api/settings", json={"sub2api_push": invalid}).status_code, 422)
            result = client.patch("/api/settings", json={"sub2api_push": {"concurrency": 9}})
            self.assertEqual(result.status_code, 200, result.text)
            self.assertEqual(client.get("/api/settings").json()["sub2api_push"]["concurrency"], 9)


class ProxyGroupTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_endpoint_uses_unpaginated_data_array(self):
        calls = []
        def handle(request):
            calls.append(request)
            return httpx.Response(200, json={"code": 0, "data": [{"id": 9, "name": "IPv6 美国", "proxy_ids": [101]}]})
        http = httpx.AsyncClient(base_url="http://fixture.invalid", transport=httpx.MockTransport(handle))
        adapter = Sub2ApiClient()
        with patch.object(adapter, "_with_client", new=AsyncMock(return_value=(http, {"x-api-key": "fixture-key"}, {}))):
            groups = await adapter.list_proxy_groups(None)
        self.assertEqual(groups[0]["id"], 9)
        self.assertEqual(str(calls[0].url), "http://fixture.invalid/api/v1/admin/proxy-groups")
        self.assertEqual(calls[0].headers["x-api-key"], "fixture-key")
