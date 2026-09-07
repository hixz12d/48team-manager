import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.jobs.workspace_sync import dispatch_workspace_sync, enqueue_all_workspace_syncs, enqueue_workspace_sync
from app.application.operations import operation_store, recover_stale_operations
from app.application.tokens import encrypt_secret
from app.application.workspace_sync import workspace_sync_service
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.identity import Account, Workspace, WorkspaceOfficialMemberSnapshot
from app.persistence.models.operations import Operation
from tests.helpers import make_client
from app.core.time import utcnow


class WorkspaceSyncScopeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name).as_posix()}/scope.db")
        await bootstrap_schema(self.engine)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.factory() as db:
            owner = Account(email="owner@example.test", local_purpose="mother", access_token_encrypted=encrypt_secret("test-access"))
            db.add(owner)
            await db.flush()
            self.owner_id = owner.id
            for i in range(8):
                db.add(Workspace(official_workspace_id=f"ws-{i}", owner_account_id=owner.id, status="active"))
            await db.commit()

    async def asyncTearDown(self):
        await self.engine.dispose()
        self.tmp.cleanup()

    async def enqueue(self, workspace_id=1):
        async with self.factory() as db:
            return await enqueue_workspace_sync(db, workspace_id)

    async def test_two_tabs_share_one_persistent_operation(self):
        a, b = await asyncio.gather(self.enqueue(), self.enqueue())
        self.assertEqual(a["operation_id"], b["operation_id"])
        self.assertEqual(sorted([a["reused"], b["reused"]]), [False, True])
        async with self.factory() as db:
            self.assertEqual(len(list(await db.scalars(select(Operation)))), 1)

    async def test_all_and_single_share_leaf_operations(self):
        single = await self.enqueue(3)
        async with self.factory() as db:
            batch = await enqueue_all_workspace_syncs(db)
            self.assertEqual(batch["queued"], 8)
            self.assertEqual(batch["reused"], 1)
            self.assertEqual(batch["items"][2]["operation_id"], single["operation_id"])
            self.assertEqual(len(list(await db.scalars(select(Operation)))), 8)

    async def test_only_requested_workspace_read_and_no_nested_operation(self):
        queued = await self.enqueue(3)
        members = AsyncMock(return_value={"success": True, "members": [{"id": "user-1", "email": "owner@example.test", "role": "owner"}], "total": 1})
        invites = AsyncMock(return_value={"success": True, "items": [], "total": 0})
        with patch.object(workspace_sync_service.workspaces.client, "get_members", members), patch.object(workspace_sync_service.workspaces.client, "get_invites", invites), patch("app.application.tokens.auth_service.refresh_account", new_callable=AsyncMock) as refresh:
            async with self.factory() as db:
                result = await dispatch_workspace_sync(db)
                self.assertTrue(result["result"]["ok"])
                self.assertEqual(result["operation_id"], queued["operation_id"])
                self.assertEqual(len(list(await db.scalars(select(Operation)))), 1)
                snapshots = list(await db.scalars(select(WorkspaceOfficialMemberSnapshot)))
                self.assertEqual([row.workspace_id for row in snapshots], [3])
            refresh.assert_not_called()
        self.assertEqual(members.await_args.args[1], "ws-2")
        self.assertEqual(invites.await_args.args[1], "ws-2")
        new = await self.enqueue(3)
        self.assertNotEqual(new["operation_id"], queued["operation_id"])

    async def test_incomplete_page_keeps_old_snapshot(self):
        await self.enqueue()
        async with self.factory() as db:
            db.add(WorkspaceOfficialMemberSnapshot(workspace_id=1, normalized_email="kept@example.test", remote_state="joined", fetched_at=utcnow()))
            await db.commit()
        with patch.object(workspace_sync_service.workspaces.client, "get_members", AsyncMock(return_value={"success": False, "members": [], "incomplete": True, "error_code": "timeout"})):
            async with self.factory() as db:
                result = await dispatch_workspace_sync(db)
                self.assertFalse(result["result"]["ok"])
                self.assertEqual([s.normalized_email for s in await db.scalars(select(WorkspaceOfficialMemberSnapshot))], ["kept@example.test"])

    async def test_member_mutation_blocks_only_its_workspace(self):
        await self.enqueue(1)
        await self.enqueue(2)
        async with self.factory() as db:
            await operation_store.create(db, op_type="rotate", workspace_id=1)
            await db.commit()
        with patch.object(workspace_sync_service, "sync_workspace", AsyncMock(return_value={"ok": True})) as sync:
            async with self.factory() as db:
                result = await dispatch_workspace_sync(db)
            self.assertTrue(result["claimed"])
            self.assertEqual(sync.await_args.args[1], 2)

    async def test_exception_is_terminal_and_does_not_leak(self):
        queued = await self.enqueue()
        with patch.object(workspace_sync_service, "sync_workspace", AsyncMock(side_effect=RuntimeError("secret-test-token"))):
            async with self.factory() as db:
                await dispatch_workspace_sync(db)
                row = await operation_store.get_by_public_id(db, queued["operation_id"])
                self.assertEqual(row.state, "failed")
                self.assertNotIn("secret-test-token", row.result_json)

    async def test_restart_requeues_read_only_job_and_migration_repeats(self):
        queued = await self.enqueue()
        async with self.factory() as db:
            row = await operation_store.get_by_public_id(db, queued["operation_id"])
            row.state = "running"
            row.locked_by = None
            await db.commit()
            await recover_stale_operations(db)
            self.assertEqual(row.state, "queued")
        await bootstrap_schema(self.engine)
        await bootstrap_schema(self.engine)
        same = await self.enqueue()
        self.assertEqual(same["operation_id"], queued["operation_id"])

    async def test_validation_never_queues_missing_credentials(self):
        async with self.factory() as db:
            owner = await db.get(Account, self.owner_id)
            owner.access_token_encrypted = None
            await db.commit()
            result = await enqueue_workspace_sync(db, 1)
            self.assertEqual(result["error_code"], "credentials_missing")
            self.assertEqual(list(await db.scalars(select(Operation))), [])


class WorkspaceSyncQueueApiTests(unittest.TestCase):
    def test_auth_and_accepted_empty_batch(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            self.assertEqual(client.post("/api/workspaces/sync").status_code, 401)
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            response = client.post("/api/workspaces/sync")
            self.assertEqual(response.status_code, 202)
            self.assertEqual(response.json()["items"], [])
