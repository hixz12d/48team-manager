import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.identity import ensure_membership, upsert_child_account, upsert_mother_account, upsert_workspace
from app.application.queries.identity import workspaces_query
from app.application.queries.portfolio import portfolio_query
from app.application.workspace_sync import WorkspaceSyncService
from app.domain.identity import LOCAL_PURPOSE_CHILD, MEMBERSHIP_STATE_INVITED, MEMBERSHIP_STATE_JOINED
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot
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
        self.assertEqual(result["remote_only"], 1)
        self.assertEqual(result["joined"], 2)
        self.assertEqual(result["invited"], 1)
        self.assertIn("官方已加入 2", result["message"])
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

    async def test_sync_promotes_local_invited_membership_when_official_has_joined(self):
        child, _ = await upsert_child_account(self.session, email="blimp_digging.1u@icloud.com", status="invited")
        await ensure_membership(
            self.session,
            workspace_id=self.workspace.id,
            account_id=child.id,
            official_role="member",
            membership_state=MEMBERSHIP_STATE_INVITED,
            local_purpose=LOCAL_PURPOSE_CHILD,
        )
        await self.session.commit()
        service = WorkspaceSyncService(
            workspaces=_FakeWorkspaces(
                members={
                    "success": True,
                    "members": [
                        {"email": "owner@example.com", "role": "account-owner", "id": "u-owner"},
                        {"email": "blimp_digging.1u@icloud.com", "role": "standard-user", "id": "u-child"},
                    ],
                    "total": 2,
                    "reported_total": 2,
                    "raw_item_count": 2,
                },
                invites={"success": True, "items": [], "total": 0},
            )
        )
        result = await service.sync_workspace(self.session, self.workspace.id)
        self.assertTrue(result["ok"])
        membership = (
            await self.session.execute(
                select(WorkspaceMembership).where(
                    WorkspaceMembership.workspace_id == self.workspace.id,
                    WorkspaceMembership.account_id == child.id,
                )
            )
        ).scalar_one()
        self.assertEqual(membership.membership_state, MEMBERSHIP_STATE_JOINED)
        self.assertIsNotNone(membership.joined_at)
        payload = await workspaces_query(self.session)
        item = payload["items"][0]
        recon = next(row for row in item["reconciliation"]["items"] if row["email"] == "blimp_digging.1u@icloud.com")
        self.assertEqual(recon["status"], "managed")
        self.assertEqual(recon["remote_state"], "joined")
        local = next(row for row in item["managed"]["accounts"] if row["email"] == "blimp_digging.1u@icloud.com")
        self.assertEqual(local["membership_state"], MEMBERSHIP_STATE_JOINED)
        portfolio = await portfolio_query(self.session)
        child_card = portfolio["groups"][0]["current_children"][0]
        self.assertEqual(child_card["email"], "blimp_digging.1u@icloud.com")
        self.assertEqual(child_card["kind"], "child")
        self.assertEqual(child_card["membership_state"], MEMBERSHIP_STATE_JOINED)


class WorkspaceSyncApiTests(unittest.TestCase):
    def test_sync_endpoint_returns_operation_id(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            response = client.post("/api/workspaces/999/sync")
            self.assertEqual(response.status_code, 404)
