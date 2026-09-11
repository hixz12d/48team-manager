import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from sqlalchemy import event
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.account_deletion import AccountDeletionError, delete_unassigned_account
from app.application.codex_export import CodexTransferError
from app.application.codex_publish import binding_status, push_accounts
from app.application.settings import get_setting_value, save_console_settings
from app.core.jwt import jwt_parser
from app.core.time import utcnow
from app.integrations.codex.client import CodexClient, normalize_url
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.codex import CodexBinding
from app.web.schemas.settings import SettingsPatch
from tests.helpers import make_client
from tests.test_codex_export import account, token


class CodexPublishTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        @event.listens_for(self.engine.sync_engine, "connect")
        def foreign_keys(connection, _):
            connection.execute("PRAGMA foreign_keys=ON")
        await bootstrap_schema(self.engine)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.item = account()
        self.db.add(self.item)
        await self.db.commit()
        self.account_id = self.item.id
        self.decrypt = patch("app.application.codex_export.decrypt_secret", side_effect=lambda value: value or "")
        self.decrypt.start()
        self.calls, self.remote = [], {}
        self.lose_import = False
        self.client = CodexClient("https://codex.example", "test-admin-key", transport=httpx.MockTransport(self.handle))

    async def asyncTearDown(self):
        self.decrypt.stop()
        await self.db.close()
        await self.engine.dispose()

    def handle(self, request):
        self.assertEqual(request.headers["x-api-key"], "test-admin-key")
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        self.assertNotIn("never-export-this-rt", request.content.decode())
        path = request.url.path
        if path.endswith("/import"):
            document = body["data"]["accounts"][0]
            self.assertNotIn("refresh_token", document)
            self.remote = {"id": "acct_test", "name": document["name"], "provider": "openai",
                           "email": document["email"], "accountId": "workspace-1", "userId": "user-1",
                           "hasRefreshToken": False, "accessTokenExpiresAt": jwt_parser.expiration_utc(document["access_token"]).isoformat()}
            if self.lose_import:
                raise httpx.ReadTimeout("SECRET SHOULD NOT LEAK")
            data = {"importedCount": 1, "accountIds": ["acct_test"]}
        elif path.endswith("/detail"):
            if not self.remote:
                return httpx.Response(404, json={"secret": "DO NOT EXPOSE"})
            data = {"account": self.remote.copy()}
        elif path.endswith("/rotate"):
            self.assertIsNone(body["refreshToken"])
            self.remote["accessTokenExpiresAt"] = jwt_parser.expiration_utc(body["accessToken"]).isoformat()
            data = {"accountId": "acct_test"}
        else:
            data = {"items": [self.remote.copy()] if self.remote else [], "page": {"page": 1, "pageSize": 100, "total": int(bool(self.remote))}}
        return httpx.Response(200, json={"code": 200, "message": "ok", "data": data})

    async def push(self):
        return await push_accounts(self.db, [self.account_id], client=self.client)

    def count(self, suffix):
        return sum(path.endswith(suffix) for _, path, _ in self.calls)

    async def test_create_then_rotate_without_duplicate_or_rt(self):
        self.assertTrue((await self.push())["ok"])
        self.item.access_token_encrypted = token(exp=int(utcnow().timestamp()) + 7200)
        self.item.credential_revision += 1
        await self.db.commit()
        self.assertTrue((await binding_status(self.db))["items"][0]["stale"])
        self.assertTrue((await self.push())["ok"])
        self.assertEqual(self.count("/import"), 1)
        self.assertEqual(self.count("/rotate"), 1)
        status = (await binding_status(self.db))["items"][0]
        self.assertFalse(status["stale"])
        self.assertNotIn("token", status)
        self.assertEqual(self.item.refresh_token_encrypted, "never-export-this-rt")

    async def test_lost_import_response_reconciles_without_duplicate(self):
        self.lose_import = True
        first = await self.push()
        self.assertFalse(first["ok"])
        self.assertNotIn("SECRET", str(first))
        second = await self.push()
        self.assertTrue(second["ok"])
        self.assertEqual(self.count("/import"), 1)

    async def test_ambiguous_missing_remote_never_reimports(self):
        self.lose_import = True
        await self.push()
        self.remote = {}
        self.assertEqual((await self.push())["results"][0]["error_code"], "import_uncertain")
        self.assertEqual(self.count("/import"), 1)

    async def test_existing_email_not_adopted(self):
        self.remote = {"id": "acct_existing", "email": self.item.email, "name": "manual import"}
        self.assertEqual((await self.push())["results"][0]["error_code"], "remote_exists")
        self.assertEqual(self.count("/import"), 0)

    async def test_remote_rt_and_identity_change_prevent_rotate(self):
        await self.push()
        self.remote["hasRefreshToken"] = True
        self.assertEqual((await self.push())["results"][0]["error_code"], "remote_refresh_owner")
        self.remote["hasRefreshToken"] = False
        self.remote["accountId"] = "other-workspace"
        self.assertEqual((await self.push())["results"][0]["error_code"], "remote_identity_mismatch")
        self.assertEqual(self.count("/rotate"), 0)

    async def test_missing_bound_remote_never_recreates(self):
        await self.push()
        self.remote = {}
        self.assertEqual((await self.push())["results"][0]["error_code"], "remote_not_found")
        self.assertEqual(self.count("/import"), 1)

    async def test_local_revision_change_reported(self):
        original = self.client.detail
        async def detail(remote_id):
            result = await original(remote_id)
            self.item.credential_revision += 1
            await self.db.commit()
            return result
        with patch.object(self.client, "detail", side_effect=detail):
            self.assertEqual((await self.push())["results"][0]["error_code"], "local_credentials_changed")

    async def test_active_lease_blocks_push_and_delete(self):
        await self.push()
        binding = await self.db.get(CodexBinding, self.account_id)
        binding.lease_until = utcnow() + timedelta(minutes=2)
        await self.db.commit()
        self.assertEqual((await self.push())["results"][0]["error_code"], "push_busy")
        with self.assertRaises(AccountDeletionError) as error:
            await delete_unassigned_account(self.db, self.account_id)
        self.assertEqual(error.exception.code, "account_busy")
        await self.db.rollback()

    async def test_local_delete_removes_only_binding(self):
        await self.push()
        count = len(self.calls)
        await delete_unassigned_account(self.db, self.account_id)
        await self.db.commit()
        self.assertIsNone(await self.db.get(CodexBinding, self.account_id))
        self.assertEqual(len(self.calls), count)
        self.assertTrue(self.remote)

    async def test_changed_target_is_blocked_before_network(self):
        with self.assertRaises(CodexTransferError):
            await push_accounts(self.db, [self.account_id], client=self.client, expected_target="https://other.example")
        self.assertEqual(self.calls, [])
        await self.push()
        self.client.base_url = "https://other.example"
        count = len(self.calls)
        self.assertEqual((await self.push())["results"][0]["error_code"], "target_changed")
        self.assertEqual(len(self.calls), count)

    async def test_partial_results_keep_success(self):
        result = await push_accounts(self.db, [self.account_id, 999], client=self.client)
        self.assertFalse(result["ok"])
        self.assertEqual(result["synced"], 1)

    async def test_secret_key_encrypted_and_not_returned(self):
        with patch("app.application.tokens.encrypt_secret", side_effect=lambda key: "encrypted:" + key), \
             patch("app.application.settings.load_console_settings", return_value={}):
            await save_console_settings(self.db, SettingsPatch(connections={
                "codex_base_url": "https://codex.example/", "codex_admin_key": "private-key"}))
            self.assertEqual(await get_setting_value(self.db, "codex_admin_key_encrypted"), "encrypted:private-key")
            with self.assertRaises(ValueError):
                await save_console_settings(self.db, SettingsPatch(connections={"codex_base_url": "https://other.example"}))
            await self.db.rollback()
            await self.push()
            with self.assertRaises(ValueError):
                await save_console_settings(self.db, SettingsPatch(connections={
                    "codex_base_url": "https://other.example", "codex_admin_key": "new-key"}))
            await self.db.rollback()


class CodexClientTests(unittest.IsolatedAsyncioTestCase):
    def test_url_validation(self):
        for url in ("https://user:pass@host", "http://example.com", "https://host/api", "https://host?a=b", "https://host#x", "file:///tmp/key", "https://host\\evil", "https://host:bad"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_url(url)
        self.assertEqual(normalize_url("https://EXAMPLE.com:443/"), "https://example.com")
        self.assertEqual(normalize_url("http://127.0.0.1:8010/"), "http://127.0.0.1:8010")

    async def test_paginated_list_is_complete_and_rejects_truncation(self):
        pages = []
        def handler(request):
            page = int(request.url.params["page"])
            pages.append(page)
            batch = [{"id": f"acct_{i}"} for i in range((page - 1) * 100, min(page * 100, 101))]
            return httpx.Response(200, json={"code": 200, "data": {"items": batch,
                "page": {"page": page, "pageSize": 100, "total": 101}}})
        client = CodexClient("https://codex.example", "test-key", transport=httpx.MockTransport(handler))
        self.assertEqual(len(await client.find_accounts("example.com")), 101)
        self.assertEqual(pages, [1, 2])
        for data in ({"items": []}, {"items": [], "page": {"page": 1, "pageSize": 100, "total": 1}}):
            client.transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"code": 200, "data": data}))
            with self.assertRaises(CodexTransferError):
                await client.find_accounts("example.com")

    async def test_redirect_and_wrong_envelope_no_retry_or_secret_errors(self):
        for response in (httpx.Response(302, headers={"location": "https://evil.example"}),
                         httpx.Response(200, json={"code": 0, "data": {}}),
                         httpx.Response(200, text="secret-token")):
            calls = []
            def handler(request):
                calls.append(request)
                return response
            client = CodexClient("https://codex.example", "secret-key", transport=httpx.MockTransport(handler))
            with self.assertRaises(CodexTransferError) as error:
                await client.find_accounts("one@example.com")
            self.assertNotIn("secret", str(error.exception))
            self.assertEqual(len(calls), 1)


class CodexPublishApiTests(unittest.TestCase):
    def test_auth_config_and_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            endpoint = "/api/accounts/codex/push"
            body = {"confirm": True, "account_ids": [1], "expected_target": "https://codex.example"}
            self.assertEqual(client.post(endpoint, json=body).status_code, 401)
            self.assertEqual(client.get("/api/accounts/codex/status").status_code, 401)
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            self.assertEqual(client.post(endpoint, json={}).status_code, 422)
            self.assertEqual(client.post(endpoint, json=body).status_code, 400)
            response = client.patch("/api/settings", json={"connections": {
                "codex_base_url": "https://codex.example", "codex_admin_key": "test-secret-key"}})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertNotIn("test-secret-key", response.text)
            self.assertEqual(response.json()["secret_state"]["codex_admin_key"], "stored")
            self.assertEqual(client.get("/api/accounts/codex/status").json(), {"items": []})
