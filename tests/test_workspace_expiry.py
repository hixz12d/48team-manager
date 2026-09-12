"""Manual dates are durable local metadata, never official subscription evidence."""
import tempfile
import unittest
from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.workspace_metadata import WorkspaceMetadataResolver
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.identity import Account, Workspace
from tests.helpers import make_client


class WorkspaceExpiryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client_context = make_client(Path(self.tmp.name))
        self.client = self.client_context.__enter__()
        self.client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{(Path(self.tmp.name) / 'team48.db').as_posix()}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            owner = Account(email="owner@example.com", local_purpose="mother", auth_state="healthy", operational_state="active")
            db.add(owner)
            await db.flush()
            ws = Workspace(name="Test", owner_account_id=owner.id, status="active", subscription_plan="business")
            db.add(ws)
            await db.commit()
            self.workspace_id = ws.id
        self.url = f"/api/workspaces/{self.workspace_id}/expiry"

    async def asyncTearDown(self):
        self.client_context.__exit__(None, None, None)
        await self.engine.dispose()
        self.tmp.cleanup()

    def save(self, value):
        response = self.client.patch(self.url, json={"expires_on": value})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["expiry"]

    async def test_save_read_update_and_clear_across_queries(self):
        initial = self.client.get("/api/workspaces").json()["items"][0]["expiry"]
        self.assertIsNone(initial["date"])
        saved = self.save("2028-02-29")
        self.assertEqual(saved["date"], "2028-02-29")
        self.assertEqual(saved["source"], "manual")
        self.assertEqual(saved["timezone"], "Asia/Shanghai")
        self.assertTrue(saved["updated_at"])
        self.assertEqual(self.client.get("/api/workspaces").json()["items"][0]["expiry"], saved)
        group = self.client.get("/api/accounts/portfolio").json()["groups"][0]
        self.assertEqual(group["expiry"], saved)
        self.assertEqual(group["subscription"]["status"], "unverified")
        self.assertEqual(self.save("2029-03-01")["date"], "2029-03-01")
        cleared = self.save(None)
        self.assertIsNone(cleared["date"])
        self.assertIsNone(cleared["source"])
        async with self.sessions() as db:
            self.assertIsNone((await db.get(Workspace, self.workspace_id)).manual_expires_on)

    async def test_invalid_or_omitted_dates_preserve_the_existing_record(self):
        self.save("2028-02-29")
        invalid = [{}, {"expires_on": ""}, {"expires_on": "2027-02-29"},
                   {"expires_on": "2028-04-31"}, {"expires_on": "0000-01-01"},
                   {"expires_on": "2028-02-29T00:00:00Z"}, {"expires_on": 1835395200},
                   {"expires_on": True}, {"expires_on": "2028-2-9"}]
        for payload in invalid:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.patch(self.url, json=payload).status_code, 422)
        async with self.sessions() as db:
            self.assertEqual((await db.get(Workspace, self.workspace_id)).manual_expires_on, date(2028, 2, 29))

    async def test_past_date_does_not_expire_official_workspace_or_account(self):
        self.save("2020-01-01")
        async with self.sessions() as db:
            ws = await db.get(Workspace, self.workspace_id)
            owner = await db.get(Account, ws.owner_account_id)
            self.assertEqual(ws.status, "active")
            self.assertEqual(ws.subscription_plan, "business")
            self.assertEqual(owner.auth_state, "healthy")
            self.assertEqual(owner.operational_state, "active")
            WorkspaceMetadataResolver().apply_resolved(ws, {"title": "Official name"}, owner_email=owner.email)
            await db.commit()
        async with self.sessions() as db:
            self.assertEqual((await db.get(Workspace, self.workspace_id)).manual_expires_on, date(2020, 1, 1))

    async def test_missing_team_returns_404(self):
        response = self.client.patch("/api/workspaces/999999/expiry", json={"expires_on": "2028-01-01"})
        self.assertEqual(response.status_code, 404)

    async def test_write_requires_admin(self):
        self.client.post("/auth/logout")
        response = self.client.patch(self.url, json={"expires_on": "2028-01-01"}, follow_redirects=False)
        self.assertIn(response.status_code, {401, 403})


class ExpiryMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_database_gains_nullable_fields_idempotently(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await bootstrap_schema(engine)
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO workspaces (name, status, name_source, version) VALUES ('Keep me', 'active', 'custom', 1)"))
                await conn.execute(text("ALTER TABLE workspaces DROP COLUMN manual_expires_on"))
                await conn.execute(text("ALTER TABLE workspaces DROP COLUMN manual_expiry_updated_at"))
            await bootstrap_schema(engine)
            await bootstrap_schema(engine)
            async with engine.connect() as conn:
                row = (await conn.execute(text("SELECT name, manual_expires_on, manual_expiry_updated_at FROM workspaces"))).one()
                self.assertEqual(tuple(row), ("Keep me", None, None))
        finally:
            await engine.dispose()


if __name__ == "__main__":
    unittest.main()
