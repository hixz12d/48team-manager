"""Official absence reconciles membership, never the credential account."""

import unittest
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.queries.identity import workspaces_query
from app.application.workspace_sync import WorkspaceSyncService
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from tests.test_workspace_sync import _FakeWorkspaces


class OfficialAbsenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.owner = Account(email="owner@example.test", local_purpose="mother")
        self.child = Account(email="child@example.test", local_purpose="child", operational_state="active",
                             access_token_encrypted="retained-ciphertext")
        self.db.add_all([self.owner, self.child])
        await self.db.flush()
        self.workspace = Workspace(official_workspace_id="ws-absence", owner_account_id=self.owner.id, status="active")
        self.db.add(self.workspace)
        await self.db.flush()
        self.membership = WorkspaceMembership(workspace_id=self.workspace.id, account_id=self.child.id,
                                               official_role="owner", membership_state="joined", local_purpose="child")
        self.binding = ExternalBinding(provider="sub2api", local_account_id=self.child.id,
                                       workspace_id=self.workspace.id, remote_account_id="123", binding_state="verified")
        self.db.add_all([self.membership, self.binding])
        await self.db.commit()
        self.metadata_patch = patch("app.application.workspace_metadata.workspace_metadata_resolver.refresh",
                                    AsyncMock(return_value={"ok": True}))
        self.metadata_patch.start()

    async def asyncTearDown(self):
        self.metadata_patch.stop()
        await self.db.close()
        await self.engine.dispose()

    async def sync(self, *, members=None, invites=None):
        service = WorkspaceSyncService(_FakeWorkspaces(
            members=members or {"success": True, "members": [{"email": self.owner.email, "role": "owner"}], "total": 1},
            invites=invites or {"success": True, "items": [], "total": 0},
        ))
        return await service.sync_workspace(self.db, self.workspace.id)

    async def test_absent_joined_member_moves_to_history_without_deleting_account(self):
        result = await self.sync()
        self.assertTrue(result["ok"])
        self.assertEqual(result["removed_memberships"], 1)
        self.assertEqual(result["local_only"], 0)
        self.assertEqual(self.membership.membership_state, "removed")
        removed_at = self.membership.removed_at
        self.assertIsNotNone(removed_at)
        self.assertEqual(self.child.access_token_encrypted, "retained-ciphertext")
        self.assertEqual(self.child.operational_state, "active")
        self.assertIsNotNone(await self.db.get(ExternalBinding, self.binding.id))
        self.assertEqual(len(list(await self.db.scalars(select(Account)))), 2)
        view = (await workspaces_query(self.db))["items"][0]
        self.assertEqual(view["member_accounts"], [])
        self.assertEqual([row["email"] for row in view["former_members"]], [self.child.email])
        self.assertNotIn(self.child.email, [row["email"] for row in view["reconciliation"]["items"]])
        repeat = await self.sync()
        self.assertEqual(repeat["removed_memberships"], 0)
        self.assertEqual(self.membership.removed_at, removed_at)

    async def test_revoked_invite_moves_to_history(self):
        self.membership.membership_state = "invited"
        await self.db.commit()
        await self.sync()
        self.assertEqual(self.membership.membership_state, "removed")

    async def test_pending_official_invite_is_preserved(self):
        result = await self.sync(invites={"success": True, "items": [{"email_address": self.child.email,
                                 "role": "account-owner", "seat_type": "prolite"}], "total": 1})
        self.assertEqual(result["removed_memberships"], 0)
        self.assertEqual(self.membership.membership_state, "invited")

    async def test_failed_invite_read_preserves_membership(self):
        result = await self.sync(invites={"success": False, "items": [], "error_code": "timeout"})
        self.assertFalse(result["ok"])
        self.assertEqual(self.membership.membership_state, "joined")

    async def test_ambiguous_counts_or_invalid_rows_never_confirm_absence(self):
        for overrides in (
            {"total": 2},
            {"reported_total": None, "total": 1},
            {"members": [{"email": self.owner.email}, {"unrecognized": "row"}], "total": 2},
        ):
            with self.subTest(overrides=overrides):
                members = {"success": True, "members": [{"email": self.owner.email}], "total": 1, **overrides}
                result = await self.sync(members=members)
                self.assertEqual(self.membership.membership_state, "joined")
                self.assertFalse(result.get("absence_reconciled", False))

    async def test_primary_owner_relationship_is_never_removed(self):
        self.membership.account_id = self.owner.id
        await self.db.commit()
        result = await self.sync(members={"success": True, "members": [], "total": 0})
        self.assertEqual(result["removed_memberships"], 0)
        self.assertEqual(self.membership.membership_state, "joined")


if __name__ == "__main__":
    unittest.main()
