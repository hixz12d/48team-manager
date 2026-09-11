import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import jwt
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.codex_export import CodexTransferError, account_document, export_document
from app.domain.automation import DEFAULT_OAUTH_CLIENT_ID
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.identity import Account
from tests.helpers import make_client


def token(email="one@example.com", **overrides):
    claims = {"exp": int(time.time()) + 3600, "email": email, "client_id": DEFAULT_OAUTH_CLIENT_ID,
              "https://api.openai.com/auth": {"chatgpt_account_id": "workspace-1", "chatgpt_user_id": "user-1"}}
    claims.update(overrides)
    return jwt.encode(claims, "test-only", algorithm="HS256")


def account(**overrides):
    fields = dict(email="one@example.com", local_purpose="standby", auth_state="healthy",
                  access_token_encrypted=token(), refresh_token_encrypted="never-export-this-rt",
                  id_token_encrypted=token(), official_account_id="workspace-1", official_user_id="user-1")
    fields.update(overrides)
    return Account(**fields)


class CodexExportTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await bootstrap_schema(self.engine)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.decrypt = patch("app.application.codex_export.decrypt_secret", side_effect=lambda value: value or "")
        self.decrypt.start()

    async def asyncTearDown(self):
        self.decrypt.stop()
        await self.db.close()
        await self.engine.dispose()

    async def test_export_no_rt_and_deduplicates(self):
        item = account()
        self.db.add(item)
        await self.db.commit()
        data = await export_document(self.db, [item.id, item.id])
        self.assertEqual(len(data["accounts"]), 1)
        self.assertEqual(set(data["accounts"][0]), {"provider", "name", "email", "access_token", "id_token"})
        self.assertNotIn("never-export-this-rt", str(data))
        self.assertEqual(item.refresh_token_encrypted, "never-export-this-rt")

    async def test_reject_missing_and_bad_credentials(self):
        for fields, code in [
            ({"access_token_encrypted": None}, "access_token_missing"),
            ({"access_token_encrypted": "bad"}, "invalid_access_token"),
            ({"access_token_encrypted": token(exp=1)}, "access_token_expired"),
            ({"access_token_encrypted": token(exp="1" * 400)}, "access_token_expired"),
            ({"access_token_encrypted": token(email="wrong@example.com")}, "identity_mismatch"),
            ({"client_id": "wrong"}, "client_id_mismatch"),
            ({"client_id": None, "access_token_encrypted": token(client_id=None)}, "client_id_mismatch"),
            ({"official_account_id": "wrong"}, "identity_mismatch"),
            ({"id_token_encrypted": token(email="wrong@example.com")}, "identity_mismatch"),
            ({"operational_state": "disabled"}, "account_disabled"),
            ({"auth_state": "oauth_required"}, "auth_required"),
        ]:
            with self.subTest(code=code), self.assertRaises(CodexTransferError) as error:
                account_document(account(**fields))
            self.assertEqual(error.exception.code, code)
        with self.assertRaises(CodexTransferError):
            await export_document(self.db, [999])

    async def test_batch_is_all_or_nothing(self):
        good, bad = account(), account(email="two@example.com", access_token_encrypted=None)
        self.db.add_all([good, bad])
        await self.db.commit()
        with self.assertRaises(CodexTransferError):
            await export_document(self.db, [good.id, bad.id])


class CodexExportApiTests(unittest.TestCase):
    def test_auth_confirmation_download_headers(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            endpoint = "/api/accounts/codex/export"
            body = {"confirm": True, "account_ids": [1]}
            self.assertEqual(client.post(endpoint, json=body).status_code, 401)
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            for invalid in ({"account_ids": [1]}, {"confirm": False, "account_ids": [1]},
                            {"confirm": True, "account_ids": []}, {"confirm": True, "account_ids": [-1]},
                            {"confirm": True, "account_ids": list(range(1, 52))},
                            {**body, "include_refresh_token": True}):
                self.assertEqual(client.post(endpoint, json=invalid).status_code, 422)
            with patch("app.web.routes.api.export_document", return_value={"accounts": []}):
                response = client.post(endpoint, json=body)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            self.assertIn("attachment", response.headers["content-disposition"])
            self.assertEqual(client.post(endpoint, json=body).status_code, 404)
