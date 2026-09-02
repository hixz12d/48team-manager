import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.console_actions import release_hme_lease_safe, retry_hme_label, run_hme_reconcile
from app.core.time import utcnow
from app.domain.resources import HME_STATE_RESERVED, HME_STATE_SIGNUP_STARTED
from app.persistence.database import Base
from app.persistence.models.resources import HmeAliasLease
from tests.helpers import make_client


class HmeActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_reconcile_is_readonly_and_release_rules(self):
        now = utcnow()
        reserved = HmeAliasLease(
            email="reserved@icloud.com",
            anonymous_id="a1",
            account_id="acc",
            local_state=HME_STATE_RESERVED,
            label_desired="team",
            label_sync_pending=False,
            expires_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        started = HmeAliasLease(
            email="started@icloud.com",
            anonymous_id="a2",
            account_id="acc",
            local_state=HME_STATE_SIGNUP_STARTED,
            label_desired="team",
            label_sync_pending=True,
            expires_at=now + timedelta(hours=1),
            created_at=now,
            updated_at=now,
        )
        self.session.add_all([reserved, started])
        await self.session.commit()

        with patch("app.application.console_actions.reconcile_aliases", return_value={"ok": True, "conflicts": 1, "findings": [{"kind": "x"}]}):
            report = await run_hme_reconcile(self.session)
        self.assertTrue(report["ok"])
        self.assertTrue(report["readonly"])
        self.assertEqual(report["conflicts"], 1)

        bad = await release_hme_lease_safe(self.session, started.id)
        self.assertFalse(bad["ok"])

        with patch("app.application.console_actions.apply_local_label", return_value=None):
            retried = await retry_hme_label(self.session, started.id)
        self.assertTrue(retried["ok"])


class HmeApiTests(unittest.TestCase):
    def test_hme_endpoints_exist(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            with patch("app.application.console_actions.reconcile_aliases", return_value={"ok": True, "conflicts": 0, "findings": []}):
                response = client.post("/api/resources/hme/reconcile")
            self.assertEqual(response.status_code, 200)
            self.assertIn("operation_id", response.json())
