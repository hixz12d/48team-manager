import tempfile
import unittest
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.identity import upsert_child_account
from app.persistence.database import Base
from tests.helpers import make_client
from tests.test_identity import WORKSPACE_UUID


class RegisterWorkspaceTests(unittest.TestCase):
    def test_register_creates_mother_and_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            denied = client.post(
                "/api/workspaces",
                json={"email": "owner@icloud.com", "official_workspace_id": WORKSPACE_UUID},
            )
            self.assertEqual(denied.status_code, 401)

            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            created = client.post(
                "/api/workspaces",
                json={
                    "email": "Owner@iCloud.com",
                    "official_workspace_id": WORKSPACE_UUID.upper(),
                    "name": "Team Alpha",
                    "seat_limit": 5,
                    "password": "mother-secret",
                    "access_token": "access-token",
                },
            )
            self.assertEqual(created.status_code, 200, created.text)
            body = created.json()
            self.assertTrue(body["ok"])
            self.assertEqual(body["workspace"]["name"], "Team Alpha")
            self.assertEqual(body["workspace"]["owner_email"], "owner@icloud.com")
            self.assertEqual(body["workspace"]["official_workspace_id"], WORKSPACE_UUID)
            self.assertEqual(body["workspace"]["seat_limit"], 5)
            self.assertEqual(body["account"]["purpose"], "mother")
            self.assertNotIn("password", body)
            self.assertNotIn("access_token", body)
            self.assertNotIn("mother-secret", created.text)

            workspaces = client.get("/api/workspaces").json()["items"]
            self.assertEqual(len(workspaces), 1)
            self.assertEqual(workspaces[0]["owner_email"], "owner@icloud.com")
            accounts = {row["email"]: row for row in client.get("/api/accounts").json()["items"]}
            self.assertEqual(accounts["owner@icloud.com"]["purpose"], "mother")
            self.assertTrue(accounts["owner@icloud.com"]["has_access_token"])
            audit = client.get("/api/identity/audit").json()
            self.assertEqual(audit["counts"]["conflict"], 0)

    def test_rejects_invalid_workspace_id_and_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            bad = client.post(
                "/api/workspaces",
                json={"email": "owner@icloud.com", "official_workspace_id": "user-abc"},
            )
            self.assertEqual(bad.status_code, 400)
            self.assertIn("Workspace ID", bad.json()["detail"])

            first = client.post(
                "/api/workspaces",
                json={"email": "owner@icloud.com", "official_workspace_id": WORKSPACE_UUID, "name": "One"},
            )
            self.assertEqual(first.status_code, 200)
            again_email = client.post(
                "/api/workspaces",
                json={
                    "email": "owner@icloud.com",
                    "official_workspace_id": "22222222-2222-2222-2222-222222222222",
                },
            )
            self.assertEqual(again_email.status_code, 409)
            again_id = client.post(
                "/api/workspaces",
                json={"email": "other@icloud.com", "official_workspace_id": WORKSPACE_UUID},
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
                    "/api/workspaces",
                    json={"email": "kid@icloud.com", "official_workspace_id": WORKSPACE_UUID},
                )
                self.assertEqual(refused.status_code, 409)
                accounts = client.get("/api/accounts").json()["items"]
                self.assertEqual(accounts[0]["purpose"], "child")
                self.assertEqual(client.get("/api/workspaces").json()["items"], [])
