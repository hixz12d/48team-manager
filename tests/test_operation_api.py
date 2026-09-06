import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.operations import operation_store
from app.persistence.database import Base
from tests.helpers import make_client


class OperationApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp.name) / "team48.db"
        self.client_cm = make_client(Path(self.tmp.name))
        self.client = self.client_cm.__enter__()
        self.client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)

    async def asyncTearDown(self):
        self.client_cm.__exit__(None, None, None)
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_detail_cancel_and_retry_whitelist(self):
        async with self.session_maker() as session:
            safe = await operation_store.create(session, op_type="quota_probe", email="a@example.com", account_id=1, workspace_id=7)
            unsafe = await operation_store.create(session, op_type="rotate", email="b@example.com")
            await operation_store.mark_step(session, safe, "probe", state="running")
            await session.commit()
            safe_id = safe.public_id
            unsafe_id = unsafe.public_id

        detail = self.client.get(f"/api/operations/{safe_id}")
        self.assertEqual(detail.status_code, 200)
        payload = detail.json()
        self.assertEqual(payload["id"], safe_id)
        self.assertTrue(payload["can_cancel"])
        self.assertTrue(any(step["step_name"] == "probe" for step in payload.get("steps") or []))

        cancel = self.client.post(f"/api/operations/{safe_id}/cancel")
        self.assertEqual(cancel.status_code, 200)
        self.assertTrue(cancel.json()["cancel_requested"])

        # force failed for retry
        async with self.session_maker() as session:
            row = await operation_store.get_by_public_id(session, safe_id)
            await operation_store.finish(session, row, {"success": False, "error": "boom", "error_code": "x"})
            bad = await operation_store.get_by_public_id(session, unsafe_id)
            await operation_store.finish(session, bad, {"success": False, "error": "boom", "error_code": "x"})
            await session.commit()

        async def _probe(db, account_id, workspace_id=None):
            self.assertEqual(workspace_id, 7)
            return {"ok": True, "operation_id": "retry1", "account_id": account_id}

        with patch("app.application.console_actions.account_quota_probe", new=_probe):
            retry_ok = self.client.post(f"/api/operations/{safe_id}/retry")
        self.assertIn(retry_ok.status_code, {200, 202})
        self.assertTrue(retry_ok.json()["ok"])

        retry_bad = self.client.post(f"/api/operations/{unsafe_id}/retry")
        self.assertEqual(retry_bad.status_code, 400)
