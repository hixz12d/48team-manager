import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.db_migrations import run_auto_migration
from app.integrations.openai.quota import (
    OpenAIQuotaClient,
    parse_wham_usage,
)
from app.models import Account, QuotaSnapshot, Team
from app.services.encryption import encryption_service
from app.services.identity import identity_service
from app.services.quota import (
    DEFAULT_QUOTA_PROBE_ENABLED,
    QuotaService,
    failure_next_quota_probe_at,
    official_overrides_sub2api_stale,
    success_next_quota_probe_at,
)


WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"


def _wham_payload(*, five=0, seven=0, five_seconds=18000, seven_seconds=604800):
    return {
        "user_id": "user-abc",
        "account_id": "acct-official",
        "email": "owner@icloud.com",
        "plan_type": "team",
        "rate_limit": {
            "allowed": True,
            "limit_reached": False,
            "primary_window": {
                "used_percent": five,
                "limit_window_seconds": five_seconds,
                "reset_after_seconds": 100,
                "reset_at": 1770000000,
            },
            "secondary_window": {
                "used_percent": seven,
                "limit_window_seconds": seven_seconds,
                "reset_after_seconds": 200,
                "reset_at": 1770600000,
            },
        },
    }


class QuotaParseTests(unittest.TestCase):
    def test_maps_windows_by_duration_not_primary_name(self):
        swapped = _wham_payload(five=12, seven=34)
        swapped["rate_limit"]["primary_window"]["limit_window_seconds"] = 604800
        swapped["rate_limit"]["primary_window"]["used_percent"] = 88
        swapped["rate_limit"]["secondary_window"]["limit_window_seconds"] = 18000
        swapped["rate_limit"]["secondary_window"]["used_percent"] = 7
        result = parse_wham_usage(swapped, now=datetime(2026, 3, 29, 12, 0, 0))
        self.assertTrue(result.success)
        self.assertEqual(result.five_hour_used_percent, 7)
        self.assertEqual(result.seven_day_used_percent, 88)
        self.assertTrue(result.can_drive_automation())

    def test_missing_windows_is_parse_error(self):
        result = parse_wham_usage({"rate_limit": {"allowed": True}}, now=datetime(2026, 3, 29, 12, 0, 0))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "parse_error")
        self.assertFalse(result.can_drive_automation())

    def test_success_slot_is_next_hour_plus_jitter(self):
        now = datetime(2026, 3, 29, 12, 10, 0)
        nxt = success_next_quota_probe_at(now, 3, jitter_seconds=0)
        self.assertEqual(nxt, datetime(2026, 3, 29, 13, 3, 0))

    def test_failure_backoff_is_5_15_30_then_60(self):
        now = datetime(2026, 3, 29, 12, 0, 0)
        self.assertEqual(failure_next_quota_probe_at(now, 1), now + timedelta(minutes=5))
        self.assertEqual(failure_next_quota_probe_at(now, 2), now + timedelta(minutes=15))
        self.assertEqual(failure_next_quota_probe_at(now, 3), now + timedelta(minutes=30))
        self.assertEqual(failure_next_quota_probe_at(now, 4), now + timedelta(minutes=60))
        self.assertEqual(failure_next_quota_probe_at(now, 9), now + timedelta(minutes=60))


class QuotaClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_failure_does_not_look_like_success(self):
        class Boom:
            async def fetch(self, access_token, db_session, account_id, identifier):
                return {"success": False, "status_code": 401, "error": "token dead", "error_code": "token_invalidated"}

        client = OpenAIQuotaClient(transport=Boom())
        result = await client.fetch_quota(access_token="tok", db_session=object(), now=datetime(2026, 3, 29, 12, 0, 0))
        self.assertFalse(result.success)
        self.assertEqual(result.error_code, "token_invalidated")
        self.assertIsNone(result.seven_day_used_percent)


class QuotaProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _seed_account(self, *, email="owner@icloud.com", token="tok", operational_state="active"):
        team = Team(
            email=email,
            access_token_encrypted=encryption_service.encrypt_token(token),
            account_id=WORKSPACE_UUID,
            team_name="Team .2026.11",
            status="active",
        )
        self.session.add(team)
        await self.session.commit()
        await identity_service.backfill(self.session)
        account = (await self.session.execute(select(Account).where(Account.email == email))).scalar_one()
        account.official_account_id = "acct-official"
        account.operational_state = operational_state
        account.next_quota_probe_at = datetime(2026, 3, 29, 11, 0, 0)
        await self.session.commit()
        return account

    async def test_disabled_by_default_does_not_probe(self):
        self.assertFalse(DEFAULT_QUOTA_PROBE_ENABLED)
        await self._seed_account()
        service = QuotaService(client=OpenAIQuotaClient(transport=AsyncMock()))
        stats = await service.run_probe_once(
            self.session,
            now=datetime(2026, 3, 29, 12, 0, 0),
            settings={"enabled": False, "stagger_minutes": 60, "batch_size": 1},
        )
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["probed"], 0)
        count = (await self.session.execute(select(QuotaSnapshot))).scalars().all()
        self.assertEqual(count, [])

    async def test_success_writes_official_snapshot_without_touching_business_state(self):
        account = await self._seed_account()

        captured = {}

        class Ok:
            async def fetch(self, access_token, db_session, account_id, identifier):
                captured["access_token"] = access_token
                captured["account_id"] = account_id
                return {"success": True, "status_code": 200, "data": _wham_payload(five=0, seven=0)}

        service = QuotaService(client=OpenAIQuotaClient(transport=Ok()))
        stats = await service.run_probe_once(
            self.session,
            now=datetime(2026, 3, 29, 12, 0, 0),
            settings={"enabled": True, "stagger_minutes": 60, "batch_size": 1},
        )
        self.assertEqual(captured["access_token"], "tok")
        self.assertEqual(captured["account_id"], "acct-official")
        self.assertEqual(stats["probed"], 1)
        snap = (await self.session.execute(select(QuotaSnapshot))).scalar_one()
        self.assertTrue(snap.success)
        self.assertEqual(snap.source, "official")
        self.assertEqual(snap.seven_day_used_percent, 0)
        self.assertEqual(snap.five_hour_used_percent, 0)
        refreshed = await self.session.get(Account, account.id)
        self.assertEqual(refreshed.operational_state, "active")
        self.assertEqual(refreshed.local_purpose, "mother")
        self.assertEqual(refreshed.quota_probe_fail_count, 0)
        self.assertTrue(official_overrides_sub2api_stale(snap, "429"))

    async def test_http_failure_writes_failed_snapshot_and_does_not_kick(self):
        account = await self._seed_account()

        class Boom:
            async def fetch(self, access_token, db_session, account_id, identifier):
                return {"success": False, "status_code": 502, "error": "upstream"}

        service = QuotaService(client=OpenAIQuotaClient(transport=Boom()))
        stats = await service.run_probe_once(
            self.session,
            now=datetime(2026, 3, 29, 12, 0, 0),
            settings={"enabled": True, "stagger_minutes": 60, "batch_size": 1},
        )
        self.assertEqual(stats["failed"], 1)
        snap = (await self.session.execute(select(QuotaSnapshot))).scalar_one()
        self.assertFalse(snap.success)
        self.assertEqual(snap.error_code, "http_5xx")
        self.assertIsNone(snap.seven_day_used_percent)
        refreshed = await self.session.get(Account, account.id)
        self.assertEqual(refreshed.operational_state, "active")
        self.assertEqual(refreshed.quota_probe_fail_count, 1)
        self.assertFalse(official_overrides_sub2api_stale(snap, "429"))

    async def test_sub2api_429_does_not_override_official_zero(self):
        official = QuotaSnapshot(
            account_id=1,
            seven_day_used_percent=0,
            five_hour_used_percent=0,
            source="official",
            queried_at=datetime(2026, 3, 29, 12, 0, 0),
            success=True,
        )
        self.assertTrue(official_overrides_sub2api_stale(official, "429"))
        stale = QuotaSnapshot(
            account_id=1,
            seven_day_used_percent=None,
            source="official",
            queried_at=datetime(2026, 3, 29, 12, 0, 0),
            success=False,
            error_code="http_429",
        )
        self.assertFalse(official_overrides_sub2api_stale(stale, "429"))


class QuotaMigrationTests(unittest.TestCase):
    def test_run_auto_migration_creates_quota_snapshots_without_dropping_legacy(self):
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
            columns = {
                row[1]
                for row in conn.execute("PRAGMA table_info(accounts)").fetchall()
            }
            conn.close()

            self.assertIn("teams", tables)
            self.assertIn("quota_snapshots", tables)
            self.assertIn("quota_slot_minute", columns)
            self.assertIn("next_quota_probe_at", columns)
            self.assertIn("quota_probe_fail_count", columns)


if __name__ == "__main__":
    unittest.main()
