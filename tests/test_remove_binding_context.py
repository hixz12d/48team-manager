"""Workspace-scoped binding resolution and bounded remove confirmation."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.rotate import RotateService
from app.domain.identity import PROVIDER_SUB2API
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding, Workspace


class BindingContextTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = self.session_maker()
        self.account = Account(email="kid@example.com", local_purpose="child", operational_state="active")
        self.db.add(self.account)
        await self.db.flush()
        self.ws1 = Workspace(official_workspace_id="ws-1", owner_account_id=self.account.id, status="active", name="A")
        self.ws2 = Workspace(official_workspace_id="ws-2", owner_account_id=self.account.id, status="active", name="B")
        self.db.add_all([self.ws1, self.ws2])
        await self.db.flush()
        sub2api = AsyncMock()
        sub2api.get_account.side_effect = lambda db, remote_id: {
            "id": remote_id, "platform": "openai", "type": "oauth",
            "email": self.account.email, "workspace_id": "ws-1" if remote_id == 101 else "ws-2",
        }
        self.rotate = RotateService(workspaces=AsyncMock(), sub2api=sub2api)

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def test_matched_scoped_binding(self):
        self.db.add(
            ExternalBinding(
                provider=PROVIDER_SUB2API,
                local_account_id=self.account.id,
                remote_account_id="101",
                workspace_id=self.ws1.id,
                binding_state="verified",
            )
        )
        self.db.add(
            ExternalBinding(
                provider=PROVIDER_SUB2API,
                local_account_id=self.account.id,
                remote_account_id="202",
                workspace_id=self.ws2.id,
                binding_state="verified",
            )
        )
        await self.db.flush()
        one = await self.rotate._remote_binding_for(self.db, self.account, workspace_id=self.ws1.id)
        two = await self.rotate._remote_binding_for(self.db, self.account, workspace_id=self.ws2.id)
        self.assertEqual(one["state"], "matched")
        self.assertEqual(one["remote_id"], "101")
        self.assertEqual(two["remote_id"], "202")
        bare = await self.rotate._remote_binding_for(self.db, self.account)
        self.assertEqual(bare["state"], "ambiguous_or_unverified")

    async def test_other_workspace_only_is_absent_for_current(self):
        self.db.add(
            ExternalBinding(
                provider=PROVIDER_SUB2API,
                local_account_id=self.account.id,
                remote_account_id="202",
                workspace_id=self.ws2.id,
                binding_state="verified",
            )
        )
        await self.db.flush()
        resolved = await self.rotate._remote_binding_for(self.db, self.account, workspace_id=self.ws1.id)
        self.assertEqual(resolved["state"], "absent")
        self.assertEqual(resolved.get("reason"), "other_workspace_only")

    async def test_confirm_member_absent_polls_until_gone(self):
        lookups = [
            ({"success": True, "lookup_state": "found"}, {"email": "kid@example.com", "status": "joined"}),
            ({"success": True, "lookup_state": "found"}, {"email": "kid@example.com", "status": "joined"}),
            ({"success": True, "lookup_state": "absent_confirmed"}, None),
        ]

        async def lookup(db, workspace, email):
            return lookups.pop(0)

        self.rotate.workspaces.lookup_live_member = lookup
        import asyncio
        from unittest.mock import patch
        with patch.object(asyncio, "sleep", new=AsyncMock()):
            result = await self.rotate._confirm_member_absent(
                self.db, workspace=self.ws1, email="kid@example.com", in_test=False
            )
        self.assertTrue(result["confirmed"])
        self.assertEqual(result["state"], "absent_confirmed")
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(lookups, [])


if __name__ == "__main__":
    unittest.main()
