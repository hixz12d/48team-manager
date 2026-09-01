import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.application.operations import operation_store
from app.application.resources.hme import (
    ClaimedAlias,
    HmeConfig,
    active_leased_emails,
    claim_next_alias,
    finalize_claim,
    mark_signup_started,
    purge_expired_leases,
    reconcile_aliases,
)
from app.application.resources.phones import phone_pool_service
from app.domain.resources import (
    FREE_ACCOUNT_LABEL,
    HME_STATE_CONSUMED,
    HME_STATE_RESERVED,
    HME_STATE_SIGNUP_STARTED,
    OUTCOME_RECENTLY_USED,
    OUTCOME_SUCCESS,
)
from app.application.resources.proxies import proxy_profile_service
from app.core.time import utcnow
from app.persistence.database import Base
from app.persistence.models.operations import Operation
from app.persistence.models.resources import HmeAliasLease, PhoneAttempt, PhonePool


class PhoneResourceTests(unittest.IsolatedAsyncioTestCase):
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

    async def _import(self, *numbers: str):
        text = "\n".join(f"{number}----https://api668.com/sms/by_key?key=abc" for number in numbers)
        return await phone_pool_service.import_lines(self.session, text)

    async def test_success_writes_attempt_and_counts(self):
        await self._import("+15551111111")
        row = await phone_pool_service.acquire(self.session, "job-1")
        await phone_pool_service.record_result(
            self.session, result=OUTCOME_SUCCESS, job_id="job-1", phone_id=row.id, purpose="signup"
        )
        row = await self.session.get(PhonePool, row.id)
        self.assertEqual(row.used_count, 1)
        attempts = list((await self.session.execute(select(PhoneAttempt))).scalars())
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0].result, OUTCOME_SUCCESS)

    async def test_recently_used_cools_without_counting(self):
        await self._import("+15551111111")
        row = await phone_pool_service.acquire(self.session, "job-1")
        await phone_pool_service.record_result(
            self.session, result=OUTCOME_RECENTLY_USED, job_id="job-1", phone_id=row.id
        )
        row = await self.session.get(PhonePool, row.id)
        self.assertEqual(int(row.used_count or 0), 0)
        self.assertEqual(row.status, "active")

    async def test_heartbeat_keeps_lease_alive_after_reserved_at_expires(self):
        await self._import("+15551111111")
        row = await phone_pool_service.acquire(self.session, "job-live")
        stale = utcnow() - timedelta(hours=1)
        row.reserved_at = stale
        row.lease_heartbeat_at = utcnow()
        await self.session.commit()
        expired = await phone_pool_service.expire_leases(self.session)
        self.assertEqual(expired, 0)
        row = await self.session.get(PhonePool, row.id)
        self.assertEqual(row.reserved_by, "job-live")


class HmeResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        self.aliases = [
            {
                "email": "one@icloud.com",
                "anonymousId": "id-one",
                "label": "别名 9",
                "active": True,
                "createdAt": "2026-01-01T00:00:00Z",
            },
            {
                "email": "two@icloud.com",
                "anonymousId": "id-two",
                "label": "",
                "active": True,
                "createdAt": "2026-01-02T00:00:00Z",
            },
        ]

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    def _claimed(self, lease: HmeAliasLease) -> ClaimedAlias:
        return ClaimedAlias(
            email=lease.email,
            anonymous_id=lease.anonymous_id,
            account_id=lease.account_id,
            lease_id=lease.id,
            job_id=lease.job_id or "",
        )

    async def test_email_otp_started_lease_is_not_reused_after_expiry(self):
        lease = HmeAliasLease(
            email="one@icloud.com",
            anonymous_id="id-one",
            account_id="acc_1",
            job_id="job-otp",
            local_state=HME_STATE_RESERVED,
            expires_at=utcnow() + timedelta(minutes=25),
            created_at=utcnow(),
        )
        self.session.add(lease)
        await self.session.commit()
        await mark_signup_started(self.session, self._claimed(lease), stage="email_otp")
        lease = await self.session.get(HmeAliasLease, lease.id)
        self.assertEqual(lease.local_state, HME_STATE_SIGNUP_STARTED)
        lease.expires_at = utcnow() - timedelta(minutes=1)
        await self.session.commit()
        await purge_expired_leases(self.session)
        await self.session.commit()
        still = await self.session.get(HmeAliasLease, lease.id)
        self.assertIsNotNone(still)
        leased = await active_leased_emails(self.session)
        self.assertIn("one@icloud.com", leased)
        cfg = HmeConfig(base_url="http://icloud-hme:8081", service_token="token-token-token", account_id="acc_1")
        with patch("app.application.resources.hme.load_config", AsyncMock(return_value=cfg)), patch(
            "app.application.resources.hme.hme_client.list_accounts",
            MagicMock(return_value=[{"id": "acc_1", "status": "active"}]),
        ), patch(
            "app.application.resources.hme.hme_client.list_aliases",
            MagicMock(return_value=self.aliases),
        ):
            nxt = await claim_next_alias(self.session, job_id="job-next")
        self.assertEqual(nxt.email, "two@icloud.com")

    async def test_created_account_push_failure_consumes_alias(self):
        lease = HmeAliasLease(
            email="one@icloud.com",
            anonymous_id="id-one",
            account_id="acc_1",
            job_id="job-push",
            local_state=HME_STATE_SIGNUP_STARTED,
            expires_at=utcnow() + timedelta(minutes=25),
            created_at=utcnow(),
        )
        self.session.add(lease)
        await self.session.commit()
        claimed = self._claimed(lease)
        with patch("app.application.resources.hme.apply_local_label", AsyncMock()) as tagged:
            await finalize_claim(
                self.session,
                claimed,
                {"success": False, "error_code": "push_failed", "occupy_alias": True},
                FREE_ACCOUNT_LABEL,
            )
            tagged.assert_awaited_once()
        leftover = await self.session.get(HmeAliasLease, lease.id)
        self.assertIsNone(leftover)

    async def test_label_failure_keeps_consumed_pending(self):
        lease = HmeAliasLease(
            email="one@icloud.com",
            anonymous_id="id-one",
            account_id="acc_1",
            job_id="job-label",
            local_state=HME_STATE_SIGNUP_STARTED,
            expires_at=utcnow() + timedelta(minutes=25),
            created_at=utcnow(),
        )
        self.session.add(lease)
        await self.session.commit()
        with patch("app.application.resources.hme.apply_local_label", AsyncMock(side_effect=RuntimeError("label down"))):
            await finalize_claim(self.session, self._claimed(lease), {"success": True}, "星尘")
        lease = await self.session.get(HmeAliasLease, lease.id)
        self.assertEqual(lease.local_state, HME_STATE_CONSUMED)
        self.assertTrue(lease.label_sync_pending)

    async def test_reconcile_reports_without_mutating_remote(self):
        lease = HmeAliasLease(
            email="kept@icloud.com",
            anonymous_id="id-k",
            account_id="acc_1",
            local_state=HME_STATE_CONSUMED,
            label_sync_pending=True,
            label_desired="星尘",
            expires_at=utcnow() + timedelta(minutes=25),
            created_at=utcnow(),
        )
        self.session.add(lease)
        await self.session.commit()
        report = await reconcile_aliases(
            self.session,
            aliases=[
                {"email": "kept@icloud.com", "anonymousId": "id-k", "label": "别名 1", "active": True},
                {"email": "ghost@icloud.com", "anonymousId": "id-g", "label": "已使用", "active": True},
            ],
        )
        kinds = {item["kind"] for item in report["findings"]}
        self.assertIn("remote_unused_local_consumed", kinds)
        self.assertIn("label_sync_pending", kinds)
        self.assertIn("remote_used_local_missing", kinds)


class ProxyFreezeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_operation_keeps_frozen_proxy_after_page_change(self):
        op = await operation_store.create(
            self.session,
            op_type="onboard",
            email="kid@icloud.com",
            input_payload={"proxy": "socks5h://10.0.0.9:1080"},
        )
        await self.session.commit()
        url, profile_id = await proxy_profile_service.freeze(
            self.session,
            job_id=op.public_id,
            form_proxy="socks5h://127.0.0.1:1080",
            mother_proxy="socks5h://127.0.0.1:1080",
        )
        self.assertEqual(url, "socks5h://127.0.0.1:1080")
        self.assertIsNotNone(profile_id)
        again, again_id = await proxy_profile_service.freeze(
            self.session,
            job_id=op.public_id,
            form_proxy="socks5h://10.0.0.9:1080",
            mother_proxy="socks5h://10.0.0.9:1080",
        )
        self.assertEqual(again, "socks5h://127.0.0.1:1080")
        self.assertEqual(again_id, profile_id)
        refreshed = await self.session.get(Operation, op.id)
        self.assertEqual(refreshed.resolved_proxy, "socks5h://127.0.0.1:1080")
