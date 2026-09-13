"""Manual counts persist per workspace and reset at Beijing midnight."""
import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.workspace_switch_count import increment_workspace_switch_count
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.identity import Workspace
from tests.helpers import make_client

CLOCK = "app.application.workspace_switch_count.utcnow"


class WorkspaceSwitchCountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.client_context = make_client(self.path)
        self.client = self.client_context.__enter__()
        self.client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{(self.path / 'team48.db').as_posix()}")
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.sessions() as db:
            workspaces = [Workspace(name=name, status="active") for name in ("First", "Second")]
            db.add_all(workspaces)
            await db.commit()
            self.ids = [workspace.id for workspace in workspaces]

    async def asyncTearDown(self):
        self.client_context.__exit__(None, None, None)
        await self.engine.dispose()
        self.tmp.cleanup()

    def increment(self, workspace_id=None):
        response = self.client.post(f"/api/workspaces/{workspace_id or self.ids[0]}/switch-count/increment")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["switch_count"]

    def records(self, portfolio=False):
        response = self.client.get("/api/accounts/portfolio" if portfolio else "/api/workspaces")
        self.assertEqual(response.status_code, 200, response.text)
        return {item["id"]: item["switch_count"] for item in response.json()["groups" if portfolio else "items"]}

    async def test_counts_are_manual_separate_and_durable(self):
        self.assertEqual(self.records()[self.ids[0]]["count"], 0)
        self.assertEqual(self.increment()["count"], 1)
        saved = self.increment()
        self.assertEqual(saved["count"], 2)
        self.assertEqual(saved["timezone"], "Asia/Shanghai")
        self.assertEqual(self.records()[self.ids[0]], saved)
        self.assertEqual(self.records(True)[self.ids[0]], saved)
        self.assertEqual(self.records()[self.ids[1]]["count"], 0)
        self.client_context.__exit__(None, None, None)
        self.client_context = make_client(self.path)
        self.client = self.client_context.__enter__()
        self.client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
        self.assertEqual(self.records()[self.ids[0]], saved)
        async with self.sessions() as db:
            workspace = await db.get(Workspace, self.ids[0])
            self.assertEqual(workspace.manual_switch_count, 2)
            self.assertEqual(workspace.status, "active")
            self.assertIsNone(workspace.last_official_sync_at)

    async def test_beijing_midnight_resets_without_a_scheduled_job(self):
        before = datetime(2026, 12, 31, 15, 59, 59, tzinfo=timezone.utc)
        midnight = datetime(2026, 12, 31, 16, 0, 0, tzinfo=timezone.utc)
        with patch(CLOCK, return_value=before):
            self.increment()
            self.assertEqual(self.increment(), {"count": 2, "date": "2026-12-31", "timezone": "Asia/Shanghai"})
        with patch(CLOCK, return_value=midnight):
            self.assertEqual(self.records()[self.ids[0]]["count"], 0)
            self.assertEqual(self.records(True)[self.ids[0]]["count"], 0)
            self.assertEqual(self.increment(), {"count": 1, "date": "2027-01-01", "timezone": "Asia/Shanghai"})
        with patch(CLOCK, return_value=datetime(2027, 1, 4, tzinfo=timezone.utc)):
            self.assertEqual(self.records()[self.ids[0]]["count"], 0)
            self.assertEqual(self.increment()["count"], 1)

    async def test_concurrent_increments_do_not_lose_counts_even_on_rollover(self):
        with patch(CLOCK, return_value=datetime(2026, 5, 1, tzinfo=timezone.utc)):
            self.increment()
        async def add_one():
            async with self.sessions() as db:
                return await increment_workspace_switch_count(db, self.ids[0])
        with patch(CLOCK, return_value=datetime(2026, 5, 2, tzinfo=timezone.utc)):
            results = await asyncio.gather(*(add_one() for _ in range(12)))
            self.assertEqual(sorted(item["switch_count"]["count"] for item in results), list(range(1, 13)))
            self.assertEqual(self.records()[self.ids[0]]["count"], 12)

    async def test_missing_team_and_authentication(self):
        self.assertEqual(self.client.post("/api/workspaces/999999/switch-count/increment").status_code, 404)
        self.client.post("/auth/logout")
        response = self.client.post(f"/api/workspaces/{self.ids[0]}/switch-count/increment", follow_redirects=False)
        self.assertIn(response.status_code, {401, 403})
        async with self.sessions() as db:
            self.assertEqual((await db.get(Workspace, self.ids[0])).manual_switch_count, 0)


class SwitchCountMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_data_survives_repeat_migration(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        try:
            await bootstrap_schema(engine)
            async with engine.begin() as conn:
                await conn.execute(text("INSERT INTO workspaces (name, status, name_source, version) VALUES ('Keep me', 'active', 'custom', 1)"))
                await conn.execute(text("ALTER TABLE workspaces DROP COLUMN manual_switch_date"))
                await conn.execute(text("ALTER TABLE workspaces DROP COLUMN manual_switch_count"))
            await bootstrap_schema(engine)
            await bootstrap_schema(engine)
            async with engine.connect() as conn:
                row = (await conn.execute(text("SELECT name, manual_switch_date, manual_switch_count FROM workspaces"))).one()
                self.assertEqual(tuple(row), ("Keep me", None, 0))
        finally:
            await engine.dispose()
