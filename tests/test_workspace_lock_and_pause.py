"""Workspace mutation lock + post-remove Sub2API pause."""

from __future__ import annotations

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.operations import operation_store
from app.application.rotate import RotateService
from app.application.tokens import encrypt_secret
from app.core.time import utcnow
from app.integrations.openai.member_adapter import (
    InviteSeatIntent,
    apply_verified_seat_wire_settings,
    build_invite_payload,
)
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership


class WorkspaceLockAndPauseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await bootstrap_schema(self.engine)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.factory()
        self.owner = Account(
            email="owner@example.com",
            local_purpose="mother",
            access_token_encrypted=encrypt_secret("owner-test"),
        )
        self.child = Account(
            email="child@example.com",
            local_purpose="child",
            operational_state="active",
            auth_state="healthy",
            access_token_encrypted=encrypt_secret("child-test"),
        )
        self.db.add_all([self.owner, self.child])
        await self.db.flush()
        self.workspace = Workspace(
            official_workspace_id="ws-1",
            owner_account_id=self.owner.id,
            status="active",
            last_official_sync_at=utcnow(),
        )
        self.db.add(self.workspace)
        await self.db.flush()
        self.db.add(
            WorkspaceMembership(
                workspace_id=self.workspace.id,
                account_id=self.child.id,
                official_role="member",
                membership_state="joined",
                local_purpose="child",
            )
        )
        self.db.add(
            ExternalBinding(
                provider="sub2api",
                local_account_id=self.child.id,
                workspace_id=self.workspace.id,
                remote_account_id="2980",
                binding_state="verified",
            )
        )
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def test_create_workspace_locked_blocks_second_holder(self):
        first, blocker = await operation_store.create_workspace_locked(
            self.db,
            op_type="kick_member",
            workspace_id=self.workspace.id,
            email=self.child.email,
        )
        self.assertIsNotNone(first)
        self.assertIsNone(blocker)
        await self.db.commit()

        second, blocker2 = await operation_store.create_workspace_locked(
            self.db,
            op_type="invite_child",
            workspace_id=self.workspace.id,
            email="other@example.com",
        )
        self.assertIsNone(second)
        self.assertIsNotNone(blocker2)
        self.assertEqual(blocker2.public_id, first.public_id)

    async def test_expired_lease_is_reclaimed(self):
        first, _ = await operation_store.create_workspace_locked(
            self.db,
            op_type="kick_member",
            workspace_id=self.workspace.id,
            email=self.child.email,
        )
        first.lease_expires_at = utcnow() - timedelta(seconds=5)
        await self.db.commit()

        second, blocker = await operation_store.create_workspace_locked(
            self.db,
            op_type="invite_child",
            workspace_id=self.workspace.id,
            email="other@example.com",
        )
        self.assertIsNotNone(second)
        self.assertIsNone(blocker)
        await self.db.refresh(first)
        self.assertEqual(first.state, "manual_required")

    async def test_kick_pauses_matched_binding_without_unbind(self):
        client = SimpleNamespace(
            get_members=AsyncMock(
                return_value={
                    "success": True,
                    "members": [{"email": self.child.email, "id": "user-child", "role": "member"}],
                    "total": 1,
                }
            ),
            get_invites=AsyncMock(return_value={"success": True, "items": [], "total": 0}),
            delete_member=AsyncMock(return_value={"success": True}),
            delete_invite=AsyncMock(return_value={"success": True}),
            pick_user_id=lambda item: item.get("id") or item.get("user_id"),
        )
        from app.application.workspaces import WorkspaceService

        workspaces = WorkspaceService(client=client)
        sub2api = SimpleNamespace(
            get_account=AsyncMock(return_value={
                "id": 2980, "email": self.child.email, "workspace_id": "ws-1",
                "platform": "openai", "type": "oauth", "schedulable": False,
            }),
            delete_accounts=AsyncMock(),
            set_account_schedulable=AsyncMock(return_value={"patched": True, "schedulable_verified": True, "schedulable": False}),
        )
        rotate = RotateService(workspaces=workspaces, sub2api=sub2api)
        # Confirm absent after kick.
        client.get_members.side_effect = [
            {
                "success": True,
                "members": [{"email": self.child.email, "id": "user-child", "role": "member"}],
                "total": 1,
            },
            {"success": True, "members": [], "total": 0},
        ]
        result = await rotate.kick_to_standby(
            self.db,
            workspace_id=self.workspace.id,
            email=self.child.email,
            unbind_sub2api=False,
            in_test=False,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result.get("paused_sub2api"))
        sub2api.set_account_schedulable.assert_awaited()
        args = sub2api.set_account_schedulable.await_args.args
        self.assertEqual(args[1], 2980)
        self.assertFalse(args[2])


class SeatWireSettingsTests(unittest.TestCase):
    def tearDown(self):
        from app.integrations.openai import member_adapter as adapter
        from app.integrations.openai.member_adapter import InviteSeatIntent

        adapter.VERIFIED_INVITE_SEAT_WIRE_VALUES.clear()
        adapter.VERIFIED_INVITE_SEAT_WIRE_VALUES[InviteSeatIntent.STANDARD] = "default"
        adapter.VERIFIED_INVITE_SEAT_WIRE_VALUES[InviteSeatIntent.PREMIUM] = "prolite"

    def test_settings_merge_enables_premium_payload(self):
        from app.integrations.openai import member_adapter as adapter

        adapter.VERIFIED_INVITE_SEAT_WIRE_VALUES.clear()
        wires = apply_verified_seat_wire_settings({"premium": "business_premium_seat"})
        payload = build_invite_payload("a@b.com", role="member", seat_intent=InviteSeatIntent.PREMIUM, wire_values=wires)
        self.assertEqual(payload.get("seat_type"), "business_premium_seat")

    def test_third_party_default_not_auto_mapped_to_premium(self):
        from app.integrations.openai import member_adapter as adapter

        adapter.VERIFIED_INVITE_SEAT_WIRE_VALUES.clear()
        payload = build_invite_payload("a@b.com", role="member", seat_intent=InviteSeatIntent.WORKSPACE_DEFAULT)
        self.assertNotIn("seat_type", payload)


if __name__ == "__main__":
    unittest.main()
