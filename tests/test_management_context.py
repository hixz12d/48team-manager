"""Cross-workspace mother/child context, linking, quota, and Sub2API names."""

from __future__ import annotations

import unittest
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.console_maintenance import link_remote_only_member
from app.application.identity import upsert_mother_account, upsert_workspace
from app.application.queries.portfolio import portfolio_query
from app.application.quota import QuotaService, snapshot_from_result
from app.application.workspace_sync import WorkspaceSyncService
from app.domain.identity import LOCAL_PURPOSE_MOTHER
from app.domain.identity.binding import canonical_sub2api_name
from app.domain.identity.policy import management_role
from app.domain.quota import QuotaResult
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace, WorkspaceOfficialMemberSnapshot


WORKSPACE_A = "11111111-1111-1111-1111-111111111111"
WORKSPACE_B = "22222222-2222-2222-2222-222222222222"


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


class ManagementContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        self.alice, _ = await upsert_mother_account(self.session, email="newxiaozhu1@gmail.com")
        self.bob, _ = await upsert_mother_account(self.session, email="hixz262@gmail.com")
        self.ws_a, _ = await upsert_workspace(
            self.session,
            source_team_id=1,
            official_workspace_id=WORKSPACE_A,
            name="Mexc1",
            subscription_plan=None,
            owner_account_id=self.alice.id,
            status="active",
            seat_limit=None,
        )
        self.ws_b, _ = await upsert_workspace(
            self.session,
            source_team_id=2,
            official_workspace_id=WORKSPACE_B,
            name="Mexc2",
            subscription_plan=None,
            owner_account_id=self.bob.id,
            status="active",
            seat_limit=None,
        )
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_cross_join_roles_and_canonical_names(self):
        self.assertEqual(management_role(self.ws_a, self.alice.id), "mother")
        self.assertEqual(management_role(self.ws_a, self.bob.id), "child")
        self.assertEqual(management_role(self.ws_b, self.bob.id), "mother")
        self.assertEqual(management_role(self.ws_b, self.alice.id), "child")
        self.assertEqual(canonical_sub2api_name("newxiaozhu1@gmail.com", "mother"), "Team（newxiaozhu1） 母号")
        self.assertEqual(canonical_sub2api_name("hixz262@gmail.com", "child"), "Team（hixz262） 子号")

        service = WorkspaceSyncService(
            workspaces=_FakeWorkspaces(
                members={
                    "success": True,
                    "members": [
                        {"email": "newxiaozhu1@gmail.com", "role": "account-owner", "id": "u-a"},
                        {"email": "hixz262@gmail.com", "role": "account-owner", "id": "u-b"},
                    ],
                    "total": 2,
                    "reported_total": 2,
                    "raw_item_count": 2,
                }
            )
        )
        result = await service.sync_workspace(self.session, self.ws_a.id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["joined_people_total"], 2)
        self.assertEqual(result["joined_member_count"], 1)
        self.assertNotIn("Owner 2", result["message"])
        self.assertIn("1 母号", result["message"])

        linked = await link_remote_only_member(self.session, self.ws_a.id, email="hixz262@gmail.com")
        self.assertTrue(linked["ok"])
        bob = await self.session.get(Account, self.bob.id)
        self.assertEqual(bob.local_purpose, LOCAL_PURPOSE_MOTHER)

        portfolio = await portfolio_query(self.session)
        group_a = next(item for item in portfolio["groups"] if item["id"] == self.ws_a.id)
        self.assertEqual(group_a["mother"]["email"], "newxiaozhu1@gmail.com")
        self.assertEqual(len(group_a["current_children"]), 1)
        self.assertEqual(group_a["current_children"][0]["email"], "hixz262@gmail.com")
        self.assertEqual(group_a["current_children"][0]["management_role"], "child")
        self.assertEqual(group_a["counts"]["managed_children"], 1)

        refused = await link_remote_only_member(self.session, self.ws_a.id, email="newxiaozhu1@gmail.com")
        self.assertFalse(refused["ok"])
        self.assertEqual(refused["error_code"], "not_linkable")

    async def test_invited_and_missing_local_account_are_not_linked(self):
        self.session.add(
            WorkspaceOfficialMemberSnapshot(
                workspace_id=self.ws_a.id,
                normalized_email="guest@example.com",
                official_role="account-owner",
                remote_state="invited",
                fetched_at=datetime.utcnow(),
            )
        )
        await self.session.commit()
        invited = await link_remote_only_member(self.session, self.ws_a.id, email="guest@example.com")
        self.assertFalse(invited["ok"])
        self.assertEqual(invited["error_code"], "not_linkable")

        self.session.add(
            WorkspaceOfficialMemberSnapshot(
                workspace_id=self.ws_a.id,
                normalized_email="missing@example.com",
                official_role="member",
                remote_state="joined",
                fetched_at=datetime.utcnow(),
            )
        )
        await self.session.commit()
        missing = await link_remote_only_member(self.session, self.ws_a.id, email="missing@example.com")
        self.assertFalse(missing["ok"])
        self.assertEqual(missing["error_code"], "no_local_account")

    async def test_quota_snapshots_are_scoped_by_workspace(self):
        now = datetime(2026, 3, 29, 12, 0, 0)
        self.session.add(
            snapshot_from_result(
                self.alice.id,
                QuotaResult(success=True, five_hour_used_percent=10, seven_day_used_percent=20, queried_at=now),
                now,
                workspace_id=self.ws_a.id,
            )
        )
        self.session.add(
            snapshot_from_result(
                self.alice.id,
                QuotaResult(success=True, five_hour_used_percent=80, seven_day_used_percent=90, queried_at=now),
                now,
                workspace_id=self.ws_b.id,
            )
        )
        await self.session.commit()
        latest = await QuotaService().latest_official_by_contexts(self.session)
        self.assertEqual(latest[(self.alice.id, self.ws_a.id)].seven_day_used_percent, 20)
        self.assertEqual(latest[(self.alice.id, self.ws_b.id)].seven_day_used_percent, 90)


if __name__ == "__main__":
    unittest.main()
