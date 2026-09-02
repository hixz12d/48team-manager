import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.identity import upsert_mother_account, upsert_workspace
from app.application.workspace_sync import WorkspaceSyncService
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace, WorkspaceOfficialMemberSnapshot
from tests.helpers import make_client


class _FakeWorkspaces:
    def __init__(self, members=None, invites=None):
        self._members = members or {"success": True, "members": [], "total": 0}
        self._invites = invites or {"success": True, "items": [], "total": 0}

    async def load_workspace(self, db, workspace_id: int):
        return await db.get(Workspace, int(workspace_id))

    async def get_members(self, db, workspace):
        return self._members

    async def get_invites(self, db, workspace):
        return self._invites

    async def owner_account(self, db, workspace):
        if workspace.owner_account_id:
            return await db.get(Account, workspace.owner_account_id)
        return None


class WorkspaceSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        mother, _ = await upsert_mother_account(self.session, email="owner@example.com")
        self.workspace, _ = await upsert_workspace(
            self.session,
            source_team_id=1,
            official_workspace_id="ws-1",
            name="Team One",
            subscription_plan=None,
            owner_account_id=mother.id,
            status="active",
            seat_limit=None,
        )
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_full_success_commits_snapshot_and_keeps_remote_only(self):
        service = WorkspaceSyncService(
            workspaces=_FakeWorkspaces(
                members={
                    "success": True,
                    "members": [
                        {"email": "owner@example.com", "role": "account-owner", "id": "u-owner"},
                        {"email": "remote-only@example.com", "role": "standard-user", "id": "u-remote"},
                    ],
                    "total": 2,
                },
                invites={
                    "success": True,
                    "items": [{"email_address": "invited@example.com", "role": "standard-user"}],
                    "total": 1,
                },
            )
        )
        result = await service.sync_workspace(self.session, self.workspace.id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["created_local_accounts"], 0)
        self.assertEqual(result["deleted_local_accounts"], 0)
        self.assertEqual(result["remote_only"], 3)
        self.assertEqual(result["matched"], 0)
        snaps = list((await self.session.execute(select(WorkspaceOfficialMemberSnapshot))).scalars())
        emails = {row.normalized_email for row in snaps}
        self.assertIn("remote-only@example.com", emails)
        self.assertIn("invited@example.com", emails)
        workspace = await self.session.get(Workspace, self.workspace.id)
        self.assertIsNotNone(workspace.last_official_sync_at)
        accounts = list((await self.session.execute(select(Account))).scalars())
        self.assertEqual(len(accounts), 1)

    async def test_partial_failure_keeps_previous_snapshot(self):
        service = WorkspaceSyncService(
            workspaces=_FakeWorkspaces(
                members={
                    "success": True,
                    "members": [{"email": "owner@example.com", "role": "account-owner", "id": "u-owner"}],
                    "total": 1,
                },
                invites={"success": True, "items": [], "total": 0},
            )
        )
        first = await service.sync_workspace(self.session, self.workspace.id)
        self.assertTrue(first["ok"])
        before = (await self.session.get(Workspace, self.workspace.id)).last_official_sync_at
        count_before = len(list((await self.session.execute(select(WorkspaceOfficialMemberSnapshot))).scalars()))

        failing = WorkspaceSyncService(
            workspaces=_FakeWorkspaces(
                members={"success": True, "members": [{"email": "owner@example.com", "role": "account-owner"}], "total": 1},
                invites={"success": False, "items": [], "error": "invite boom", "error_code": "invites_failed"},
            )
        )
        second = await failing.sync_workspace(self.session, self.workspace.id)
        self.assertFalse(second["ok"])
        after = (await self.session.get(Workspace, self.workspace.id)).last_official_sync_at
        count_after = len(list((await self.session.execute(select(WorkspaceOfficialMemberSnapshot))).scalars()))
        self.assertEqual(before, after)
        self.assertEqual(count_before, count_after)


class WorkspaceSyncApiTests(unittest.TestCase):
    def test_sync_endpoint_returns_operation_id(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            response = client.post("/api/workspaces/999/sync")
            self.assertEqual(response.status_code, 404)
