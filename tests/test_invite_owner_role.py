"""Owner invite mapping, pending-role mismatch, and last-owner protection."""

from __future__ import annotations

import unittest

from sqlalchemy import select

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.console_maintenance import add_local_child, delete_local_workspace, update_workspace_member_role
from app.application.identity import upsert_mother_account, upsert_workspace
from app.application.rotate import RotateService
from app.application.tokens import encrypt_secret
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership


WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"


class _InviteWS:
    def __init__(self, *, live_item=None, invite_ok=True):
        self.live_item = live_item
        self.invite_ok = invite_ok
        self.invites = []
        self.role_updates = []

    async def load_workspace(self, db, workspace_id: int):
        return await db.get(Workspace, int(workspace_id))

    async def owner_account(self, db, workspace):
        if workspace.owner_account_id:
            return await db.get(Account, workspace.owner_account_id)
        return None

    async def lookup_live_member(self, db, workspace, email):
        if self.live_item and self.live_item.get("email") == email:
            return {"success": True, "lookup_state": "found", "members": [self.live_item]}, self.live_item
        return {"success": True, "lookup_state": "absent_confirmed", "members": []}, None

    async def invite_member(self, db, workspace_id, email, role="owner"):
        self.invites.append({"email": email, "role": role})
        if not self.invite_ok:
            return {"success": False, "error": "invite rejected", "error_code": "invite_failed"}
        return {"success": True, "message": f"已邀请 {email}", "requested_role": role}

    async def update_member_role(self, db, workspace_id, email, role="owner", *, user_id=None):
        self.role_updates.append({"email": email, "role": role, "user_id": user_id})
        live = self.live_item or {}
        if live.get("email") == email and live.get("status") == "joined":
            existing = live.get("role") or "member"
            if existing == role:
                return {"success": True, "already": True, "email": email, "role": role, "existing_role": existing, "message": f"{email} 官方角色已经是 {role}"}
            live["role"] = role
            return {"success": True, "email": email, "role": role, "existing_role": existing, "user_id": user_id or live.get("user_id"), "message": f"已把 {email} 改成官方 {role}"}
        return {"success": False, "error": f"{email} 还没加入官方席位，不能改已加入成员的角色", "error_code": "not_joined"}

    def _last_owner_guard(self, workspace, owner, live, live_item, *, email):
        from app.application.workspaces import WorkspaceService

        return WorkspaceService()._last_owner_guard(workspace, owner, live, live_item, email=email)


class InviteOwnerRoleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        mother, _ = await upsert_mother_account(self.session, email="owner@example.com")
        mother.access_token_encrypted = encrypt_secret("tok")
        self.workspace, _ = await upsert_workspace(
            self.session,
            source_team_id=1,
            official_workspace_id=WORKSPACE_UUID,
            name="Team One",
            subscription_plan=None,
            owner_account_id=mother.id,
            status="active",
            seat_limit=5,
        )
        await self.session.commit()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_manual_invite_defaults_to_owner(self):
        workspaces = _InviteWS()
        added = await add_local_child(
            self.session,
            self.workspace.id,
            email="kid@example.com",
            workspaces=workspaces,
        )
        self.assertTrue(added["ok"])
        self.assertEqual(workspaces.invites, [{"email": "kid@example.com", "role": "owner"}])
        child = await self.session.get(Account, added["account_id"])
        membership = (
            await self.session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.account_id == child.id)
            )
        ).scalar_one()
        self.assertEqual(membership.official_role, "owner")
        self.assertEqual(child.local_purpose, "child")
        self.assertEqual(self.workspace.owner_account_id, (await self.session.get(Workspace, self.workspace.id)).owner_account_id)

    async def test_manual_invite_explicit_member(self):
        workspaces = _InviteWS()
        added = await add_local_child(
            self.session,
            self.workspace.id,
            email="member@example.com",
            workspaces=workspaces,
            role="member",
        )
        self.assertTrue(added["ok"])
        self.assertEqual(workspaces.invites, [{"email": "member@example.com", "role": "member"}])

    async def test_pending_invite_role_mismatch_does_not_pretend_success(self):
        workspaces = _InviteWS(live_item={"email": "kid@example.com", "status": "invited", "role": "member"})
        failed = await add_local_child(
            self.session,
            self.workspace.id,
            email="kid@example.com",
            workspaces=workspaces,
            role="owner",
        )
        self.assertFalse(failed["ok"])
        self.assertEqual(failed["error_code"], "invite_role_mismatch")
        self.assertEqual(workspaces.invites, [])

    async def test_second_official_owner_stays_local_child(self):
        workspaces = _InviteWS(live_item={"email": "second@example.com", "status": "joined", "role": "owner", "user_id": "u-2"})
        added = await add_local_child(
            self.session,
            self.workspace.id,
            email="second@example.com",
            workspaces=workspaces,
        )
        self.assertTrue(added["ok"])
        child = await self.session.get(Account, added["account_id"])
        workspace = await self.session.get(Workspace, self.workspace.id)
        self.assertEqual(child.local_purpose, "child")
        self.assertNotEqual(workspace.owner_account_id, child.id)

    async def test_last_official_owner_cannot_be_kicked(self):
        mother = await self.session.get(Account, self.workspace.owner_account_id)
        workspaces = _InviteWS(
            live_item={"email": mother.email, "status": "joined", "role": "owner", "user_id": "u-owner"}
        )
        service = RotateService(workspaces=workspaces)
        result = await service.kick_to_standby(self.session, workspace_id=self.workspace.id, email=mother.email, in_test=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "primary_mother_protected")

    async def test_joined_member_can_be_promoted_to_owner(self):
        workspaces = _InviteWS(live_item={"email": "kid@example.com", "status": "joined", "role": "member", "user_id": "u-kid"})
        added = await add_local_child(
            self.session,
            self.workspace.id,
            email="kid@example.com",
            workspaces=workspaces,
            role="member",
        )
        self.assertTrue(added["ok"])
        updated = await update_workspace_member_role(
            self.session,
            self.workspace.id,
            email="kid@example.com",
            role="owner",
            workspaces=workspaces,
        )
        self.assertTrue(updated["ok"])
        self.assertEqual(workspaces.role_updates, [{"email": "kid@example.com", "role": "owner", "user_id": None}])
        membership = (
            await self.session.execute(
                select(WorkspaceMembership).where(WorkspaceMembership.account_id == added["account_id"])
            )
        ).scalar_one()
        self.assertEqual(membership.official_role, "owner")
        child = await self.session.get(Account, added["account_id"])
        self.assertEqual(child.local_purpose, "child")

    async def test_delete_local_workspace_keeps_official_team(self):
        workspaces = _InviteWS(live_item={"email": "kid@example.com", "status": "joined", "role": "member", "user_id": "u-kid"})
        added = await add_local_child(
            self.session,
            self.workspace.id,
            email="kid@example.com",
            workspaces=workspaces,
            role="member",
        )
        workspace_id = self.workspace.id
        owner_id = self.workspace.owner_account_id
        deleted = await delete_local_workspace(self.session, workspace_id)
        self.assertTrue(deleted["ok"])
        self.assertIsNone(await self.session.get(Workspace, workspace_id))
        self.assertIsNone(await self.session.get(Account, added["account_id"]))
        self.assertIsNone(await self.session.get(Account, owner_id))


if __name__ == "__main__":
    unittest.main()
