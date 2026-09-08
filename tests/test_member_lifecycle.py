import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.console_maintenance import add_local_child
from app.application.queries.identity import workspaces_query
from app.application.rotate import RotateService
from app.application.tokens import encrypt_secret
from app.application.workspaces import WorkspaceService
from app.core.time import utcnow
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot


class MemberLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await bootstrap_schema(self.engine)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.factory()
        self.owner = Account(email="owner@example.com", local_purpose="mother", access_token_encrypted=encrypt_secret("owner-test"))
        self.child = Account(email="child@example.com", local_purpose="child", operational_state="active", auth_state="healthy", access_token_encrypted=encrypt_secret("child-test"))
        self.db.add_all([self.owner, self.child])
        await self.db.flush()
        self.workspace = Workspace(official_workspace_id="ws-1", owner_account_id=self.owner.id, status="active", last_official_sync_at=utcnow())
        self.db.add(self.workspace)
        await self.db.flush()
        self.membership = WorkspaceMembership(workspace_id=self.workspace.id, account_id=self.child.id, official_role="member", membership_state="joined", local_purpose="child")
        self.db.add(self.membership)
        self.db.add(WorkspaceOfficialMemberSnapshot(workspace_id=self.workspace.id, normalized_email=self.child.email, official_user_id="user-child", official_role="member", remote_state="joined", fetched_at=utcnow()))
        self.db.add(ExternalBinding(provider="sub2api", local_account_id=self.child.id, workspace_id=self.workspace.id, remote_account_id="23", binding_state="verified"))
        await self.db.commit()
        self.joined = {"success": True, "members": [{"email": self.child.email, "id": "user-child", "role": "member"}], "total": 1}
        self.absent = {"success": True, "members": [], "total": 0}
        self.client = SimpleNamespace(
            get_members=AsyncMock(side_effect=[self.joined, self.absent]),
            get_invites=AsyncMock(return_value={"success": True, "items": [], "total": 0}),
            delete_member=AsyncMock(return_value={"success": True}),
            delete_invite=AsyncMock(return_value={"success": True}),
            send_invite=AsyncMock(return_value={"success": True}),
        )
        self.workspaces = WorkspaceService(client=self.client)
        self.sub2api = SimpleNamespace(
            delete_accounts=AsyncMock(),
            get_account=AsyncMock(return_value={
                "id": 23, "email": self.child.email, "workspace_id": "ws-1",
                "platform": "openai", "type": "oauth", "schedulable": False,
            }),
        )
        self.rotate = RotateService(workspaces=self.workspaces, sub2api=self.sub2api)

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def test_kick_then_reinvite_preserves_account_credentials_and_binding(self):
        before = self.child.access_token_encrypted
        result = await self.rotate.kick_to_standby(self.db, workspace_id=self.workspace.id, email=self.child.email, reason="console_team_detail",  in_test=True)
        self.assertTrue(result["success"])
        await self.db.commit()
        self.assertEqual(self.membership.membership_state, "removed")
        self.assertEqual(self.child.local_purpose, "standby")
        self.assertEqual(list(await self.db.scalars(select(WorkspaceOfficialMemberSnapshot))), [])
        group = (await workspaces_query(self.db))["items"][0]
        self.assertEqual(group["former_members"][0]["id"], self.child.id)
        self.assertTrue(group["former_members"][0]["can_reinvite"])
        self.client.get_members.side_effect = None
        self.client.get_members.return_value = self.absent
        invited = await add_local_child(self.db, self.workspace.id, email=self.child.email, role="member", workspaces=self.workspaces)
        self.assertTrue(invited["ok"])
        self.assertFalse(invited["created"])
        self.assertEqual(invited["account_id"], self.child.id)
        self.assertEqual(self.membership.membership_state, "invited")
        self.assertIsNone(self.membership.removed_at)
        refreshed = (await workspaces_query(self.db))["items"][0]
        self.assertEqual(refreshed["former_members"], [])
        pending = next(item for item in refreshed["reconciliation"]["items"] if item["email"] == self.child.email)
        self.assertEqual(pending["status"], "invited")
        self.assertEqual(self.child.access_token_encrypted, before)
        self.assertEqual(len(list(await self.db.scalars(select(Account)))), 2)
        self.assertEqual(len(list(await self.db.scalars(select(ExternalBinding)))), 1)
        self.sub2api.delete_accounts.assert_not_called()
        self.client.send_invite.assert_awaited_once()

    async def test_unverified_delete_keeps_membership_and_snapshot(self):
        self.client.get_members.side_effect = [self.joined] + [{"success": False, "error": "timeout"}] * 5
        result = await self.rotate.kick_to_standby(self.db, workspace_id=self.workspace.id, email=self.child.email,  in_test=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "kick_unverified")
        self.assertEqual(self.membership.membership_state, "joined")
        self.assertEqual(self.child.local_purpose, "child")
        self.assertEqual(len(list(await self.db.scalars(select(WorkspaceOfficialMemberSnapshot)))), 1)

    async def test_already_absent_reconciles_local_snapshot(self):
        self.client.get_members.side_effect = None
        self.client.get_members.return_value = self.absent
        result = await self.rotate.kick_to_standby(self.db, workspace_id=self.workspace.id, email=self.child.email,  in_test=True)
        self.assertTrue(result["success"])
        self.client.delete_member.assert_not_called()
        self.assertEqual(self.membership.membership_state, "removed")
        self.assertEqual(list(await self.db.scalars(select(WorkspaceOfficialMemberSnapshot))), [])

    async def test_other_workspace_context_is_not_changed(self):
        other = Workspace(official_workspace_id="ws-2", owner_account_id=self.owner.id)
        self.db.add(other)
        await self.db.flush()
        membership = WorkspaceMembership(workspace_id=other.id, account_id=self.child.id, membership_state="joined", local_purpose="child", official_role="member")
        self.db.add(membership)
        await self.db.commit()
        result = await self.rotate.kick_to_standby(self.db, workspace_id=self.workspace.id, email=self.child.email,  in_test=True)
        self.assertTrue(result["success"])
        self.assertEqual(self.child.local_purpose, "child")
        self.assertEqual(self.child.operational_state, "active")
        self.assertEqual(membership.membership_state, "joined")

    async def test_failed_lookup_never_sends_reinvite(self):
        self.client.get_members.side_effect = None
        self.client.get_members.return_value = {"success": False, "error": "timeout"}
        result = await add_local_child(self.db, self.workspace.id, email=self.child.email, role="member", workspaces=self.workspaces)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "invite_lookup_unknown")
        self.client.send_invite.assert_not_called()
        self.assertEqual(self.membership.membership_state, "joined")

    async def test_revoke_cannot_kick_member_who_has_accepted_invitation(self):
        result = await self.rotate.kick_to_standby(self.db, workspace_id=self.workspace.id, email=self.child.email, invitation_only=True,  in_test=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "already_joined")
        self.client.delete_member.assert_not_called()
        self.client.delete_invite.assert_not_called()

    async def test_revoke_verified_clears_snapshot_but_keeps_account(self):
        self.client.get_members.side_effect = None
        self.client.get_members.return_value = self.absent
        self.client.get_invites.side_effect = [
            {"success": True, "items": [{"email": self.child.email, "role": "member", "id": "user-child"}], "total": 1},
            {"success": True, "items": [], "total": 0},
        ]
        result = await self.rotate.kick_to_standby(self.db, workspace_id=self.workspace.id, email=self.child.email, invitation_only=True,  in_test=True)
        self.assertTrue(result["success"])
        self.client.delete_invite.assert_awaited_once()
        self.assertEqual(self.membership.membership_state, "removed")
        self.assertEqual(list(await self.db.scalars(select(WorkspaceOfficialMemberSnapshot))), [])
        self.assertIsNotNone(await self.db.get(Account, self.child.id))

    async def test_invite_operation_records_success_not_failed(self):
        from app.application.console_actions import invite_workspace_child
        from app.application.operations import operation_store
        with patch("app.application.console_actions.add_local_child", AsyncMock(return_value={"ok": True, "account_id": self.child.id})):
            result = await invite_workspace_child(self.db, self.workspace.id, email=self.child.email, role="member")
        row = await operation_store.get_by_public_id(self.db, result["operation_id"])
        self.assertEqual(row.state, "success")
        self.assertTrue(result["success"])

    async def test_failed_remote_delete_preserves_binding_and_account(self):
        self.client.get_members.side_effect = None
        self.client.get_members.return_value = self.absent
        for receipt in ({"deleted": [], "failed": [{"id": 23, "status": 500}]},
                        {"deleted": [23], "failed": [{"id": 23, "status": 500}]},
                        {"deleted": [99], "failed": []}):
            self.sub2api.delete_accounts.return_value = receipt
            result = await self.rotate.kick_to_standby(
                self.db, workspace_id=self.workspace.id, email=self.child.email,
                purge_local=True, in_test=True,
            )
            self.assertTrue(result["partial"])
            self.assertFalse(result["purged"])
            self.assertFalse(result["unbound_sub2api"])
            self.assertEqual(len(list(await self.db.scalars(select(ExternalBinding)))), 1)
            self.assertIsNotNone(await self.db.get(Account, self.child.id))
        self.client.delete_member.assert_not_awaited()

    async def test_disabled_account_is_not_reinvited(self):
        self.child.operational_state = "disabled"
        await self.db.commit()
        result = await add_local_child(self.db, self.workspace.id, email=self.child.email, role="member", workspaces=self.workspaces)
        self.assertEqual(result["error_code"], "account_unavailable")
        self.client.send_invite.assert_not_called()
        self.client.get_members.assert_not_called()
