import asyncio
import unittest
from datetime import timedelta

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import PhonePool
from app.services.phone_pool import (
    OUTCOME_INVALID,
    OUTCOME_NO_SMS,
    OUTCOME_RECENTLY_USED,
    OUTCOME_RISK,
    OUTCOME_SUCCESS,
    PhonePoolEmpty,
    phone_pool_service,
)
from app.utils.time_utils import get_now


class PhonePoolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine(
            "sqlite+aiosqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _import(self, *numbers: str, url: str = "https://api668.com/sms/by_key?key=abc"):
        text = "\n".join(f"{number}----{url}" for number in numbers)
        return await phone_pool_service.import_lines(self.session, text)

    async def test_import_skips_invalid_and_reports(self):
        result = await phone_pool_service.import_lines(
            self.session,
            "\n".join(
                [
                    "+15551111111----https://api668.com/sms/by_key?key=a",
                    "not-a-phone",
                    "+15551111111----https://api668.com/sms/by_key?key=a",
                    "+15552222222----ftp://bad",
                    "",
                    "# comment",
                    "+15553333333----https://api668.com/sms/by_key?key=b",
                ]
            ),
        )
        self.assertEqual(result["imported"], 2)
        self.assertEqual(result["skipped"], 3)
        self.assertTrue(result["errors"])

    async def test_acquire_order_and_lease(self):
        await self._import("+15551111111", "+15552222222")
        first = await phone_pool_service.acquire(self.session, "job-a")
        second = await phone_pool_service.acquire(self.session, "job-b")
        self.assertEqual(first.number, "+15551111111")
        self.assertEqual(second.number, "+15552222222")
        self.assertEqual(first.reserved_by, "job-a")
        self.assertEqual(int(first.used_count or 0), 0)

    async def test_success_counts_and_maxed(self):
        await self._import("+15551111111")
        for index in range(3):
            row = await phone_pool_service.acquire(self.session, f"job-{index}")
            await phone_pool_service.record_result(
                self.session, result=OUTCOME_SUCCESS, job_id=f"job-{index}", phone_id=row.id
            )
            if index < 2:
                row = await self.session.get(PhonePool, row.id)
                row.last_used_at = get_now() - timedelta(hours=2)
                await self.session.commit()
        row = await self.session.get(PhonePool, 1)
        self.assertEqual(row.used_count, 3)
        self.assertEqual(row.status, "maxed")
        with self.assertRaises(PhonePoolEmpty):
            await phone_pool_service.acquire(self.session, "job-x")

    async def test_cooldown_skips_recent_success(self):
        await self._import("+15551111111", "+15552222222")
        row = await phone_pool_service.acquire(self.session, "job-1")
        await phone_pool_service.record_result(
            self.session, result=OUTCOME_SUCCESS, job_id="job-1", phone_id=row.id
        )
        nxt = await phone_pool_service.acquire(self.session, "job-2")
        self.assertEqual(nxt.number, "+15552222222")

    async def test_invalid_disables_without_counting(self):
        await self._import("+15551111111", "+15552222222")
        row = await phone_pool_service.acquire(self.session, "job-1")
        await phone_pool_service.record_result(
            self.session,
            result=OUTCOME_INVALID,
            job_id="job-1",
            phone_id=row.id,
            message="already linked to the maximum number of accounts",
        )
        row = await self.session.get(PhonePool, row.id)
        self.assertEqual(row.status, "disabled")
        self.assertEqual(int(row.used_count or 0), 0)
        nxt = await phone_pool_service.acquire(self.session, "job-2")
        self.assertEqual(nxt.number, "+15552222222")

    async def test_recently_used_and_risk_do_not_burn(self):
        await self._import("+15551111111")
        row = await phone_pool_service.acquire(self.session, "job-1")
        await phone_pool_service.record_result(
            self.session, result=OUTCOME_RECENTLY_USED, job_id="job-1", phone_id=row.id
        )
        row = await self.session.get(PhonePool, row.id)
        self.assertEqual(row.status, "active")
        self.assertEqual(int(row.used_count or 0), 0)
        self.assertIsNone(row.reserved_by)

        row.last_used_at = get_now() - timedelta(hours=2)
        await self.session.commit()
        row = await phone_pool_service.acquire(self.session, "job-2")
        await phone_pool_service.record_result(self.session, result=OUTCOME_RISK, job_id="job-2", phone_id=row.id)
        row = await self.session.get(PhonePool, row.id)
        self.assertEqual(row.status, "risk")
        self.assertEqual(int(row.used_count or 0), 0)
        row.last_used_at = get_now() - timedelta(hours=2)
        row.status = "active"
        await self.session.commit()
        row = await phone_pool_service.acquire(self.session, "job-3")
        await phone_pool_service.record_result(self.session, result=OUTCOME_RISK, job_id="job-3", phone_id=row.id)
        row = await self.session.get(PhonePool, row.id)
        self.assertEqual(row.status, "disabled")

    async def test_no_sms_two_times_goes_risk(self):
        await self._import("+15551111111")
        row = await phone_pool_service.acquire(self.session, "job-1")
        await phone_pool_service.record_result(self.session, result=OUTCOME_NO_SMS, job_id="job-1", phone_id=row.id)
        row = await self.session.get(PhonePool, row.id)
        self.assertEqual(row.status, "active")
        row = await phone_pool_service.acquire(self.session, "job-2")
        await phone_pool_service.record_result(self.session, result=OUTCOME_NO_SMS, job_id="job-2", phone_id=row.id)
        row = await self.session.get(PhonePool, row.id)
        self.assertEqual(row.status, "risk")
        self.assertEqual(int(row.used_count or 0), 0)

    async def test_cancel_releases_lease(self):
        await self._import("+15551111111")
        row = await phone_pool_service.acquire(self.session, "job-1")
        self.assertEqual(row.reserved_by, "job-1")
        released = await phone_pool_service.release(self.session, job_id="job-1")
        self.assertEqual(released, 1)
        row = await self.session.get(PhonePool, row.id)
        self.assertIsNone(row.reserved_by)
        again = await phone_pool_service.acquire(self.session, "job-2")
        self.assertEqual(again.number, "+15551111111")

    async def test_concurrent_jobs_do_not_share_number(self):
        await self._import("+15551111111")

        async def take(job_id: str):
            async with self.session_maker() as session:
                try:
                    row = await phone_pool_service.acquire(session, job_id)
                    return row.number
                except PhonePoolEmpty as exc:
                    return str(exc)

        first, second = await asyncio.gather(take("job-a"), take("job-b"))
        values = {first, second}
        self.assertIn("+15551111111", values)
        self.assertTrue(any("号码池" in item or item == "+15551111111" for item in values))
        self.assertEqual(len([item for item in (first, second) if item == "+15551111111"]), 1)

    async def test_empty_pool_message(self):
        with self.assertRaises(PhonePoolEmpty) as ctx:
            await phone_pool_service.acquire(self.session, "job-1")
        self.assertIn("号码池为空", str(ctx.exception))

        await self._import("+15551111111")
        row = await phone_pool_service.acquire(self.session, "job-cool")
        await phone_pool_service.record_result(
            self.session, result=OUTCOME_SUCCESS, job_id="job-cool", phone_id=row.id
        )
        with self.assertRaises(PhonePoolEmpty) as ctx:
            await phone_pool_service.acquire(self.session, "job-2")
        self.assertTrue("冷却" in str(ctx.exception) or "用尽" in str(ctx.exception) or "没有可用" in str(ctx.exception))

    async def test_manual_phone_does_not_touch_pool(self):
        await self._import("+15551111111")

        async def attempt(number, sms_url):
            return OUTCOME_SUCCESS

        result = await phone_pool_service.run_attempts(
            self.session,
            job_id="job-1",
            attempt=attempt,
            phone_line="+15559999999----https://api668.com/sms/by_key?key=manual",
        )
        self.assertTrue(result["ok"])
        self.assertFalse(result["from_pool"])
        self.assertEqual(result["number"], "+15559999999")
        row = await phone_pool_service.acquire(self.session, "job-2")
        self.assertEqual(row.number, "+15551111111")
        self.assertEqual(int(row.used_count or 0), 0)

    async def test_retry_invalid_then_success(self):
        await self._import("+15551111111", "+15552222222")
        outcomes = {"+15551111111": OUTCOME_INVALID, "+15552222222": OUTCOME_SUCCESS}

        async def attempt(number, sms_url):
            return outcomes[number]

        logs = []
        result = await phone_pool_service.run_attempts(
            self.session,
            job_id="job-1",
            attempt=attempt,
            on_log=lambda stage, message: logs.append(message),
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["number"], "+15552222222")
        self.assertEqual(result["tried"], ["+15551111111", "+15552222222"])
        first = await self.session.get(PhonePool, 1)
        self.assertEqual(first.status, "disabled")
        self.assertEqual(int(first.used_count or 0), 0)
        self.assertTrue(any("领取" in item for item in logs))
        self.assertTrue(any("换号原因" in item for item in logs))
