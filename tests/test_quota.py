import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.quota import QuotaService
from app.core.crypto import token_cipher
from app.domain.quota import (
    DEFAULT_QUOTA_PROBE_ENABLED,
    due_quota_account_ids,
    failure_next_quota_probe_at,
    official_overrides_sub2api_stale,
    parse_wham_usage,
    quota_probe_user_message,
    success_next_quota_probe_at,
)
from app.integrations.openai.quota import OpenAIQuotaClient
from app.persistence.database import Base
from app.persistence.models.identity import Account
from app.persistence.models.quota import QuotaSnapshot
from tests.helpers import make_client


WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"


def _wham_payload(*, five=0, seven=0, five_seconds=18000, seven_seconds=604800):
    return {
        "rate_limit": {
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
        }
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

    def test_due_ids_compare_naive_sqlite_times_with_aware_now(self):
        now = datetime(2026, 9, 3, 12, 20, tzinfo=timezone.utc)

        class Row:
            def __init__(self, account_id, scheduled):
                self.id = account_id
                self.next_quota_probe_at = scheduled

        due = due_quota_account_ids(
            [
                Row(9, datetime(2026, 9, 3, 12, 55, 25)),
                Row(6, datetime(2026, 9, 3, 12, 15, 59)),
            ],
            now,
            limit=3,
        )
        self.assertEqual(due, [6])

    def test_quota_probe_user_message_maps_token_revoked_json(self):
        raw = '{\n  "error": {\n    "message": "Encountered invalidated oauth token for user, failing request",\n    "type": null,\n    "code": "token_revoked",\n    "param": null\n  },\n  "status": 401\n}'
        self.assertEqual(
            quota_probe_user_message("token_revoked", raw),
            "官方登录已失效，点「授权」用这个邮箱重新登录后再读额度。",
        )
        self.assertIn("还没授权", quota_probe_user_message("missing_token", "local access token missing"))


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

    async def _seed_account(self):
        account = Account(
            email="owner@icloud.com",
            official_plan="unknown",
            local_purpose="mother",
            operational_state="active",
            auth_state="unknown",
            official_account_id="acct-official",
            access_token_encrypted=token_cipher().encrypt("tok"),
            next_quota_probe_at=datetime(2026, 3, 29, 11, 0, 0),
        )
        self.session.add(account)
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
        self.assertEqual(list((await self.session.execute(select(QuotaSnapshot))).scalars()), [])

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
        refreshed = await self.session.get(Account, account.id)
        self.assertEqual(refreshed.operational_state, "active")
        self.assertFalse(official_overrides_sub2api_stale(snap, "429"))

    async def test_display_keeps_last_successful_snapshot_after_failed_probe(self):
        account = await self._seed_account()
        now = datetime(2026, 3, 29, 12, 0, 0)
        from app.application.quota import snapshot_from_result
        from app.domain.quota import QuotaResult

        self.session.add(
            snapshot_from_result(
                account.id,
                QuotaResult(
                    success=True,
                    five_hour_used_percent=12,
                    seven_day_used_percent=34,
                    queried_at=now,
                ),
                now,
                workspace_id=1,
            )
        )
        later = now + timedelta(hours=1)
        self.session.add(
            snapshot_from_result(
                account.id,
                QuotaResult(
                    success=False,
                    error_code="token_revoked",
                    error_message="Encountered invalidated oauth token",
                    queried_at=later,
                ),
                later,
                workspace_id=1,
            )
        )
        await self.session.commit()
        service = QuotaService()
        latest = await service.latest_official_by_accounts(self.session)
        displayed = await service.latest_official_by_accounts(self.session, success_only=True)
        self.assertFalse(latest[account.id].success)
        self.assertEqual(latest[account.id].error_code, "token_revoked")
        self.assertTrue(displayed[account.id].success)
        self.assertEqual(displayed[account.id].five_hour_used_percent, 12)
        self.assertEqual(displayed[account.id].seven_day_used_percent, 34)
        scoped = await service.latest_official_by_contexts(self.session, success_only=True)
        self.assertEqual(scoped[(account.id, 1)].seven_day_used_percent, 34)


class QuotaConsoleTests(unittest.TestCase):
    def test_accounts_api_shows_official_quota(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            payload = client.get("/api/accounts").json()
            self.assertEqual(payload["items"], [])
