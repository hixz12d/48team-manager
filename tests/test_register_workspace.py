import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import jwt
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.identity import upsert_child_account
from app.core.jwt import jwt_parser
from app.persistence.database import Base
from tests.helpers import make_client
from tests.test_identity import WORKSPACE_UUID


def fake_access_token(email="owner@icloud.com", workspace_id=WORKSPACE_UUID, role="account-owner"):
    return jwt.encode(
        {
            "email": email,
            "https://api.openai.com/auth": {
                "user_id": "user-owner",
                "chatgpt_account_id": workspace_id,
                "organizations": [
                    {
                        "id": workspace_id,
                        "title": "Team Alpha",
                        "role": role,
                    }
                ],
            },
        },
        "test",
        algorithm="HS256",
    )


class FakeOAuthClient:
    def __init__(self, *, email="owner@icloud.com", workspace_id=WORKSPACE_UUID):
        self.email = email
        self.workspace_id = workspace_id

    async def exchange_oauth_code(self, **kwargs):
        token = fake_access_token(self.email, self.workspace_id)
        return {
            "success": True,
            "access_token": token,
            "refresh_token": "refresh-token",
            "id_token": token,
        }


class JwtOrgTests(unittest.TestCase):
    def test_extracts_workspace_from_organizations(self):
        token = fake_access_token()
        self.assertEqual(jwt_parser.extract_email(token), "owner@icloud.com")
        self.assertEqual(jwt_parser.extract_chatgpt_account_id(token), WORKSPACE_UUID)
        orgs = jwt_parser.extract_organizations(token)
        self.assertEqual(orgs[0]["id"], WORKSPACE_UUID)


class RegisterWorkspaceTests(unittest.TestCase):
    def test_oauth_registers_mother_from_callback(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            denied = client.post(
                "/api/workspaces/oauth/start",
                json={"email": "owner@icloud.com"},
            )
            self.assertEqual(denied.status_code, 401)

            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            started = client.post(
                "/api/workspaces/oauth/start",
                json={"email": "Owner@iCloud.com", "proxy": "socks5://127.0.0.1:1080"},
            )
            self.assertEqual(started.status_code, 200, started.text)
            body = started.json()
            self.assertTrue(body["ok"])
            self.assertTrue(body["ticket"])
            self.assertIn("auth.openai.com/oauth/authorize", body["authorize_url"])
            self.assertEqual(body["email"], "owner@icloud.com")
            self.assertIn("login_hint=", body["authorize_url"])

            fake = FakeOAuthClient()
            with patch("app.application.commands.workspaces.chatgpt_client", fake):
                completed = client.post(
                    "/api/workspaces/oauth/complete",
                    json={
                        "ticket": body["ticket"],
                        "callback_url": "http://localhost:1455/auth/callback?code=oauth-code",
                    },
                )
            self.assertEqual(completed.status_code, 200, completed.text)
            created = completed.json()
            self.assertTrue(created["ok"])
            self.assertEqual(created["workspace"]["name"], "Team Alpha")
            self.assertEqual(created["workspace"]["owner_email"], "owner@icloud.com")
            self.assertEqual(created["workspace"]["official_workspace_id"], WORKSPACE_UUID)
            self.assertEqual(created["account"]["purpose"], "mother")
            self.assertNotIn("access_token", created)
            self.assertNotIn("refresh_token", created)
            self.assertNotIn("oauth-code", completed.text)

            workspaces = client.get("/api/workspaces").json()["items"]
            self.assertEqual(len(workspaces), 1)
            self.assertEqual(workspaces[0]["owner_email"], "owner@icloud.com")
            accounts = {row["email"]: row for row in client.get("/api/accounts").json()["items"]}
            self.assertEqual(accounts["owner@icloud.com"]["purpose"], "mother")
            self.assertTrue(accounts["owner@icloud.com"]["has_access_token"])
            self.assertEqual(accounts["owner@icloud.com"]["proxy"], "set")
            self.assertIsNotNone(accounts["owner@icloud.com"]["proxy_profile_id"])
            self.assertTrue(str(accounts["owner@icloud.com"].get("proxy_url") or "").startswith("socks5"))
            self.assertEqual(workspaces[0].get("owner_proxy_set"), True)
            self.assertEqual(workspaces[0].get("member_accounts"), [])
            audit = client.get("/api/identity/audit").json()
            self.assertEqual(audit["counts"]["conflict"], 0)

            again = client.post(
                "/api/workspaces/oauth/start",
                json={"email": "owner@icloud.com"},
            )
            self.assertEqual(again.status_code, 409)

    def test_oauth_rejects_missing_code_and_duplicate_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            first = client.post("/api/workspaces/oauth/start", json={"email": "owner@icloud.com"})
            ticket = first.json()["ticket"]
            missing = client.post(
                "/api/workspaces/oauth/complete",
                json={"ticket": ticket, "callback_url": "http://localhost:1455/auth/callback"},
            )
            self.assertEqual(missing.status_code, 400)
            self.assertIn("授权码", missing.json()["detail"])

            fake = FakeOAuthClient()
            with patch("app.application.commands.workspaces.chatgpt_client", fake):
                created = client.post(
                    "/api/workspaces/oauth/complete",
                    json={
                        "ticket": ticket,
                        "callback_url": "http://localhost:1455/auth/callback?code=oauth-code",
                    },
                )
            self.assertEqual(created.status_code, 200, created.text)

            second = client.post("/api/workspaces/oauth/start", json={"email": "other@icloud.com"})
            self.assertEqual(second.status_code, 200)
            with patch("app.application.commands.workspaces.chatgpt_client", FakeOAuthClient(email="other@icloud.com")):
                again_id = client.post(
                    "/api/workspaces/oauth/complete",
                    json={
                        "ticket": second.json()["ticket"],
                        "callback_url": "http://localhost:1455/auth/callback?code=oauth-code-2",
                    },
                )
            self.assertEqual(again_id.status_code, 409)


class RegisterWorkspaceGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_does_not_promote_existing_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            engine = create_async_engine(f"sqlite+aiosqlite:///{(tmp_path / 'team48.db').as_posix()}")
            factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
            async with engine.begin() as conn:
                await conn.run_sync(Base.metadata.create_all)
            async with factory() as session:
                await upsert_child_account(session, email="kid@icloud.com", status="active")
                await session.commit()
            await engine.dispose()

            with make_client(tmp_path) as client:
                client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
                refused = client.post(
                    "/api/workspaces/oauth/start",
                    json={"email": "kid@icloud.com"},
                )
                self.assertEqual(refused.status_code, 409)
                accounts = client.get("/api/accounts").json()["items"]
                self.assertEqual(accounts[0]["purpose"], "child")
                self.assertEqual(client.get("/api/workspaces").json()["items"], [])
