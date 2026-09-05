import unittest
import asyncio
from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.application.jobs.dispatcher import ReauthDispatcher
from app.application.operations import operation_store
from app.persistence.database import Base
from app.persistence.models.operations import Operation


class ReauthDispatcherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    async def asyncTearDown(self):
        await self.engine.dispose()

    async def test_dispatcher_claims_queued_reauth_and_executes_it(self):
        async with self.factory() as db:
            row = await operation_store.create(
                db,
                op_type="reauth",
                account_id=1,
                email="child@icloud.com",
                input_payload={"ticket": "ticket-1"},
                state="queued",
                source="auto",
            )
            public_id = row.public_id
            await db.commit()

        async def fake_run_job(db, operation_id, ticket):
            current = await operation_store.get_by_public_id(db, operation_id)
            self.assertEqual(current.state, "running")
            self.assertEqual(ticket, "ticket-1")
            await operation_store.finish(db, current, {"success": True, "status": "success"})
            await db.commit()
            return {"success": True}

        dispatcher = ReauthDispatcher(poll_seconds=0.01, heartbeat_seconds=0.01)
        dispatcher._factory = self.factory
        with patch("app.application.jobs.dispatcher.reauth_service.run_job", side_effect=fake_run_job):
            result = await dispatcher.dispatch_once()
        self.assertTrue(result["claimed"])
        async with self.factory() as db:
            finished = await db.get(Operation, row.id)
            self.assertEqual(finished.state, "success")
            self.assertIsNone(finished.locked_by)
            self.assertIsNone(finished.lease_expires_at)

    async def test_cancel_waits_for_execution_task_to_exit(self):
        async with self.factory() as db:
            row = await operation_store.create(
                db,
                op_type="reauth",
                email="child@icloud.com",
                input_payload={"ticket": "ticket-cancel"},
                state="queued",
            )
            row_id = row.id
            await db.commit()

        exited = asyncio.Event()
        started = asyncio.Event()

        async def slow_run_job(db, operation_id, ticket):
            started.set()
            try:
                await asyncio.sleep(30)
            finally:
                exited.set()

        dispatcher = ReauthDispatcher(poll_seconds=0.01, heartbeat_seconds=0.01)
        dispatcher._factory = self.factory
        with patch("app.application.jobs.dispatcher.reauth_service.run_job", side_effect=slow_run_job):
            dispatch = asyncio.create_task(dispatcher.dispatch_once())
            await asyncio.wait_for(started.wait(), timeout=2)
            async with self.factory() as db:
                current = await db.get(Operation, row_id)
                current.cancel_requested = True
                await db.commit()
            result = await dispatch
        self.assertTrue(result["cancelled"])
        self.assertTrue(exited.is_set())
        async with self.factory() as db:
            cancelled = await db.get(Operation, row_id)
            self.assertEqual(cancelled.state, "cancelled")
            self.assertIsNone(cancelled.locked_by)

    async def test_queued_job_is_not_browser_busy_or_recovered(self):
        async with self.factory() as db:
            row = await operation_store.create(
                db,
                op_type="reauth",
                email="child@icloud.com",
                input_payload={"ticket": "ticket-2"},
                state="queued",
            )
            await db.commit()
            self.assertIsNone(await operation_store.browser_busy(db))
            recovered = await operation_store.recover_stale(db, reclaim_all_active=True)
            self.assertEqual(recovered, [])
            await db.refresh(row)
            self.assertEqual(row.state, "queued")
