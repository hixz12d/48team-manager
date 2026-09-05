import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.operations import operation_store, pack_input, recover_stale_operations, unpack_input
from app.core.time import utcnow
from app.persistence.database import Base
from app.persistence.models.operations import Operation
from tests.helpers import make_client


class OperationStoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_password_is_encrypted_in_input_json(self):
        packed = pack_input({"email": "kid@icloud.com", "password": "secret-pass"})
        self.assertNotIn("secret-pass", packed)
        unpacked = unpack_input(packed)
        self.assertEqual(unpacked["password"], "secret-pass")
        self.assertEqual(unpacked["email"], "kid@icloud.com")

    async def test_recover_stale_does_not_steal_unexpired_foreign_lease(self):
        future = utcnow() + timedelta(minutes=10)
        row = await operation_store.create(
            self.session,
            op_type="quota_probe",
            email="kid@icloud.com",
            input_payload={"email": "kid@icloud.com"},
        )
        row.locked_by = "old-host:1"
        row.lease_expires_at = future
        await self.session.commit()
        recovered = await operation_store.recover_stale(self.session, reclaim_all_active=False)
        self.assertEqual(len(recovered), 0)
        refreshed = await self.session.get(Operation, row.id)
        self.assertEqual(refreshed.state, "running")
        self.assertEqual(refreshed.locked_by, "old-host:1")

    async def test_finish_keeps_manual_required(self):
        row = await operation_store.create(self.session, op_type="reauth", email="kid@icloud.com")
        await operation_store.finish(
            self.session,
            row,
            {"success": False, "error": "ticket gone", "error_code": "oauth_expired", "status": "manual_required"},
        )
        await self.session.commit()
        refreshed = await self.session.get(Operation, row.id)
        self.assertEqual(refreshed.state, "manual_required")

    async def test_restart_does_not_resume_browser_or_rotate(self):
        onboard = await operation_store.create(self.session, op_type="onboard", email="kid@icloud.com")
        rotate = await operation_store.create(self.session, op_type="rotate", email="old@icloud.com")
        await self.session.commit()
        stats = await recover_stale_operations(self.session)
        self.assertEqual(stats["recovered"], 2)
        self.assertEqual(stats["manual"], 2)
        self.assertEqual((await self.session.get(Operation, onboard.id)).state, "manual_required")
        self.assertEqual((await self.session.get(Operation, rotate.id)).state, "manual_required")

    async def test_kicked_step_stays_success_after_recover(self):
        row = await operation_store.create(self.session, op_type="rotate", email="kid@icloud.com")
        await operation_store.mark_step(self.session, row, "kicked", state="success", result={"status": "standby"})
        await self.session.commit()
        await operation_store.recover_stale(self.session)
        self.assertTrue(await operation_store.step_succeeded(self.session, row, "kicked"))

    async def test_note_on_queued_job_does_not_take_a_lease(self):
        row = await operation_store.create(
            self.session,
            op_type="reauth",
            email="kid@icloud.com",
            input_payload={"ticket": "ticket-note"},
            state="queued",
        )
        await operation_store.note(self.session, row, "queued", "iCloud queued", touch_lease=False)
        await self.session.commit()
        refreshed = await self.session.get(Operation, row.id)
        self.assertEqual(refreshed.state, "queued")
        self.assertIsNone(refreshed.locked_by)
        self.assertIsNone(refreshed.lease_expires_at)


class OperationConsoleTests(unittest.TestCase):
    def test_operations_api_lists_persisted_jobs(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            payload = client.get("/api/operations").json()
            self.assertEqual(payload["items"], [])
