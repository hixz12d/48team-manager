import os
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.db_migrations import run_auto_migration
from app.models import Operation, Team
from app.services.child_accounts import child_account_service
from app.services.onboard import OnboardService
from app.services.operations import (
    pack_input,
    unpack_input,
    operation_store,
)
from app.services import onboard_jobs
from app.utils.time_utils import get_now


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

    async def test_recover_stale_reclaims_active_without_waiting_for_lease(self):
        future = get_now() + timedelta(minutes=10)
        row = await operation_store.create(
            self.session,
            op_type="onboard",
            team_id=1,
            email="kid@icloud.com",
            input_payload={"email": "kid@icloud.com", "team_id": 1},
        )
        row.locked_by = "old-host:1"
        row.lease_expires_at = future
        await self.session.commit()

        recovered = await operation_store.recover_stale(self.session, reclaim_all_active=True)
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].state, "waiting")
        self.assertIsNone(recovered[0].locked_by)

    async def test_finish_keeps_manual_required(self):
        row = await operation_store.create(self.session, op_type="reauth", team_id=1, email="kid@icloud.com")
        await operation_store.finish(
            self.session,
            row,
            {
                "success": False,
                "error": "ticket gone",
                "error_code": "oauth_expired",
                "status": "manual_required",
            },
        )
        await self.session.commit()
        refreshed = await self.session.get(Operation, row.id)
        self.assertEqual(refreshed.state, "manual_required")

    async def test_active_for_workspace_excludes_self(self):
        first = await operation_store.create(
            self.session,
            op_type="rotate",
            team_id=7,
            email="old@icloud.com",
            input_payload={"team_id": 7},
        )
        second = await operation_store.create(
            self.session,
            op_type="onboard",
            team_id=7,
            email="new@icloud.com",
            input_payload={"team_id": 7},
        )
        await self.session.commit()
        busy = await operation_store.active_for_workspace(self.session, 7, actions=("rotate", "onboard"))
        self.assertEqual(busy.public_id, second.public_id)
        same = await operation_store.active_for_workspace(
            self.session,
            7,
            actions=("rotate", "onboard"),
            exclude_public_id=second.public_id,
        )
        self.assertEqual(same.public_id, first.public_id)
        none = await operation_store.active_for_workspace(self.session, 8)
        self.assertIsNone(none)


class RotateIdempotencyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_resume_after_kicked_does_not_kick_again(self):
        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            account_id="acc-1",
            max_members=5,
            current_members=2,
            proxy="socks5h://127.0.0.1:1080",
            status="active",
            account_role="account-owner",
        )
        self.session.add(team)
        await self.session.flush()
        child = await child_account_service.upsert_from_input(self.session, email="kid@example.com")
        await child_account_service.mark_standby(self.session, child)
        await self.session.commit()

        op = await operation_store.create(
            self.session,
            op_type="rotate",
            team_id=team.id,
            email="kid@example.com",
            input_payload={"team_id": team.id, "email": "kid@example.com", "reason": "weekly_limit"},
        )
        await operation_store.mark_step(
            self.session,
            op,
            "kicked",
            state="success",
            result={"success": True, "status": "standby"},
        )
        await self.session.commit()

        service = OnboardService()
        service._load_team = AsyncMock(return_value=team)
        service.kick_to_standby = AsyncMock(side_effect=AssertionError("should not kick again"))
        service.invite_and_onboard = AsyncMock(return_value={"success": True, "child": {"email": "new@example.com"}})

        result = await service.kick_and_refill(
            self.session,
            team_id=team.id,
            email="kid@example.com",
            job_id=op.public_id,
            reason="weekly_limit",
            force_refill=True,
        )
        self.assertTrue(result["success"])
        self.assertTrue(result["kick"]["skipped_duplicate_kick"])
        service.kick_to_standby.assert_not_called()
        service.invite_and_onboard.assert_awaited()

    async def test_manual_kick_conflicts_with_running_rotate(self):
        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            account_id="acc-1",
            max_members=5,
            current_members=2,
            proxy="socks5h://127.0.0.1:1080",
            status="active",
        )
        self.session.add(team)
        await self.session.flush()
        await operation_store.create(
            self.session,
            op_type="rotate",
            team_id=team.id,
            email="old@icloud.com",
            input_payload={"team_id": team.id, "email": "old@icloud.com"},
        )
        await self.session.commit()
        service = OnboardService()
        service._kick_to_standby_impl = AsyncMock(side_effect=AssertionError("should not kick while rotate holds lock"))
        result = await service.kick_to_standby(self.session, team_id=team.id, email="old@icloud.com")
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "operation_conflict")
        service._kick_to_standby_impl.assert_not_called()


class OperationMigrationTests(unittest.TestCase):
    def test_run_auto_migration_creates_operations_without_dropping_legacy(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "legacy.db")
            import sqlite3

            conn = sqlite3.connect(db_path)
            conn.execute(
                """
                CREATE TABLE teams (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email VARCHAR(255) NOT NULL,
                    access_token_encrypted TEXT NOT NULL
                )
                """
            )
            conn.commit()
            conn.close()

            run_auto_migration(db_path)

            conn = sqlite3.connect(db_path)
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            conn.close()
            self.assertIn("teams", tables)
            self.assertIn("operations", tables)
            self.assertIn("operation_steps", tables)


class OnboardJobCompatTests(unittest.TestCase):
    def test_create_job_still_works_in_memory_for_tests(self):
        job = onboard_jobs.create_job(team_id=1, email="free@example.com", action="free_register")
        busy = onboard_jobs.any_running(onboard_jobs.BROWSER_ACTIONS)
        self.assertIsNotNone(busy)
        self.assertEqual(busy["id"], job["id"])
        onboard_jobs.finish(job["id"], {"success": True})
        self.assertIsNone(onboard_jobs.active_job_for_email("free@example.com"))


if __name__ == "__main__":
    unittest.main()
