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
        context_patch = patch("app.application.workspace_metadata.workspace_metadata_resolver.client.get_account_context",
                              new_callable=AsyncMock)
        self.context = context_patch.start()
        self.addCleanup(context_patch.stop)
        self.official_ids = [f"00000000-0000-0000-0000-{i:012d}" for i in range(8)]
        self.context.return_value = {"success": True, "data": {"accounts": {
            self.official_ids[i]: {"account": {"name": f"Official Team {i}"}} for i in range(8)
        }}}
        async with self.factory() as db:
            owner = Account(email="owner@example.test", local_purpose="mother", access_token_encrypted=encrypt_secret("test-access"))
            db.add(owner)
            await db.flush()
            self.owner_id = owner.id
            for i in range(8):
                db.add(Workspace(official_workspace_id=self.official_ids[i], owner_account_id=owner.id, status="active"))
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

    async def _sync_target(self):
        queued = await self.enqueue(3)
        members = AsyncMock(return_value={"success": True, "members": [{"id": "user-1", "email": "owner@example.test", "role": "owner"}], "total": 1})
        invites = AsyncMock(return_value={"success": True, "items": [], "total": 0})
        with patch.object(workspace_sync_service.workspaces.client, "get_members", members), patch.object(workspace_sync_service.workspaces.client, "get_invites", invites), patch("app.application.tokens.auth_service.refresh_account", new_callable=AsyncMock) as refresh:
            async with self.factory() as db:
                count_before = len(list(await db.scalars(select(Operation))))
                result = await dispatch_workspace_sync(db)
                self.assertTrue(result["result"]["ok"])
                self.assertEqual(result["operation_id"], queued["operation_id"])
                self.assertEqual(len(list(await db.scalars(select(Operation)))), count_before)
                snapshots = list(await db.scalars(select(WorkspaceOfficialMemberSnapshot)))
                self.assertEqual([row.workspace_id for row in snapshots], [3])
            refresh.assert_not_called()
        self.assertEqual(members.await_args.args[1], self.official_ids[2])
        self.assertEqual(invites.await_args.args[1], self.official_ids[2])
        return result

    async def test_only_requested_workspace_read_and_no_nested_operation(self):
        queued = await self._sync_target()
        async with self.factory() as db:
            self.assertEqual(len(list(await db.scalars(select(Operation)))), 1)
        new = await self.enqueue(3)
        self.assertNotEqual(new["operation_id"], queued["operation_id"])

    async def test_queued_sync_refreshes_only_target_name_and_prefers_live_context(self):
        with patch("app.application.workspace_metadata.workspace_metadata_resolver._jwt_orgs",
                   return_value=[{"id": self.official_ids[2], "name": "Old token name"}]):
            result = await self._sync_target()
        self.assertEqual(result["result"]["status"], "success")
        self.assertEqual(self.context.await_count, 1)
        self.assertEqual(self.context.await_args.kwargs["account_id"], self.official_ids[2])
        async with self.factory() as db:
            target = await db.get(Workspace, 3)
            self.assertEqual(target.official_name, "Official Team 2")
            self.assertEqual(target.name, "Official Team 2")
            self.assertIsNotNone(target.official_name_synced_at)
            self.assertIsNone(target.official_name_last_error)
            self.assertIsNone((await db.get(Workspace, 2)).official_name)

    async def test_queued_sync_preserves_custom_name(self):
        async with self.factory() as db:
            target = await db.get(Workspace, 3)
            target.name = target.custom_name = "Local Team"
            await db.commit()
        await self._sync_target()
        async with self.factory() as db:
            target = await db.get(Workspace, 3)
            self.assertEqual(target.official_name, "Official Team 2")
            self.assertEqual(target.name, "Local Team")
            self.assertEqual(target.name_source, "custom")

    async def test_metadata_miss_or_timeout_preserves_name_and_member_sync(self):
        for response in ({"success": True, "data": {"id": self.official_ids[0], "name": "Wrong Team"}},
                         {"success": True, "data": {"unexpected": True}}, TimeoutError()):
            with self.subTest(response=response):
                async with self.factory() as db:
                    target = await db.get(Workspace, 3)
                    target.name = target.official_name = "Existing Team"
                    await db.commit()
                self.context.side_effect = response if isinstance(response, Exception) else None
                self.context.return_value = response
                result = await self._sync_target()
                self.assertEqual(result["result"]["status"], "partial")
                self.assertTrue(result["result"]["warnings"])
                async with self.factory() as db:
                    target = await db.get(Workspace, 3)
                    self.assertEqual(target.name, "Existing Team")
                    self.assertEqual(target.official_name, "Existing Team")
                    self.assertTrue(target.official_name_last_error)
                    finished = list(await db.scalars(select(Operation).where(Operation.state == "partial")))
                    self.assertTrue(finished)
                    self.assertIn("warnings", finished[-1].result_json)

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
