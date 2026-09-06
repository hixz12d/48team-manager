import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.quota import QuotaService
from app.application.tokens import AuthService
from app.core.crypto import token_cipher
from app.core.time import as_utc, utcnow
from app.domain.quota import QuotaResult
from app.integrations.openai.chatgpt import ChatGPTClient
from app.persistence.database import Base
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.identity import Account
from app.persistence.models.operations import Operation
from app.persistence.models.quota import CredentialLease, QuotaProbeState, QuotaSnapshot
from app.persistence.models.settings import SystemSetting


class DurableProbeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name).as_posix()}/test.db")
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = self.factory()
        self.account = Account(email="test@example.com", local_purpose="child", operational_state="active", auth_state="healthy",
            access_token_encrypted=token_cipher().encrypt("old-access"), refresh_token_encrypted=token_cipher().encrypt("refresh"))
        self.db.add(self.account)
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def test_concurrent_enqueue_has_one_null_context_and_operation(self):
        async def queue():
            async with self.factory() as db:
                account = await db.get(Account, self.account.id)
                return await QuotaService().enqueue(db, account)
        results = await asyncio.gather(queue(), queue())
        self.assertEqual(results[0]["operation_id"], results[1]["operation_id"])
        self.assertEqual(len(list((await self.db.execute(select(QuotaProbeState))).scalars())), 1)
        self.assertEqual(len(list((await self.db.execute(select(Operation))).scalars())), 1)

    async def test_global_dispatch_spacing_applies_across_workers(self):
        service = QuotaService(AsyncMock(fetch_quota=AsyncMock(return_value=QuotaResult(True, five_hour_used_percent=20))))
        now = utcnow()
        await service.enqueue(self.db, self.account, now=now)
        await service.run_queued_once(self.db, now=now)
        await service.enqueue(self.db, self.account, now=now + timedelta(seconds=1))
        await service.run_queued_once(self.db, now=now + timedelta(seconds=2))
        self.assertEqual(service.client.fetch_quota.await_count, 1)
        await service.run_queued_once(self.db, now=now + timedelta(seconds=21))
        self.assertEqual(service.client.fetch_quota.await_count, 2)

    async def test_refresh_network_failure_does_not_require_oauth(self):
        client = AsyncMock(refresh_access_token=AsyncMock(return_value={"success": False, "status_code": 429, "retry_after": "7200"}))
        now = utcnow()
        result = await AuthService(client).refresh_account(self.db, self.account, now=now)
        self.assertFalse(result["allow_oauth"])
        self.assertEqual(self.account.auth_state, "healthy")
        self.assertEqual(self.account.credential_revision, 1)
        lease = await self.db.get(CredentialLease, self.account.id)
        self.assertEqual(as_utc(lease.next_attempt_at), now + timedelta(hours=2))

    async def test_refresh_lock_is_account_scoped_and_persistent(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def refresh(*args, **kwargs):
            started.set()
            await release.wait()
            return {"success": True, "access_token": "replacement-access"}
        client = AsyncMock(refresh_access_token=AsyncMock(side_effect=refresh))
        task = asyncio.create_task(AuthService(client).refresh_account(self.db, self.account))
        await started.wait()
        async with self.factory() as other:
            account = await other.get(Account, self.account.id)
            result = await AuthService(client).refresh_account(other, account)
            self.assertEqual(result["error_code"], "refresh_deferred")
        release.set()
        await task
        self.assertEqual(client.refresh_access_token.await_count, 1)
        self.assertEqual(self.account.credential_revision, 2)

    async def test_refresh_late_tokens_cannot_overwrite_manual_credentials(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def refresh(*args, **kwargs):
            started.set()
            await release.wait()
            return {"success": True, "access_token": "late-access"}
        service = AuthService(AsyncMock(refresh_access_token=AsyncMock(side_effect=refresh)))
        task = asyncio.create_task(service.refresh_account(self.db, self.account))
        await started.wait()
        async with self.factory() as other:
            account = await other.get(Account, self.account.id)
            account.access_token_encrypted = token_cipher().encrypt("manual-access")
            await other.commit()
        release.set()
        result = await task
        self.assertEqual(result["error_code"], "credential_revision_conflict")
        self.assertEqual(token_cipher().decrypt(self.account.access_token_encrypted), "manual-access")

    async def test_actual_401_refreshes_once_and_validates_new_version(self):
        auth_client = AsyncMock(refresh_access_token=AsyncMock(return_value={"success": True, "access_token": "new-access"}))
        quota_client = AsyncMock(fetch_quota=AsyncMock(side_effect=[
            QuotaResult(False, http_status=401, request_count=1),
            QuotaResult(True, http_status=200, five_hour_used_percent=12, request_count=1),
        ]))
        service = QuotaService(quota_client)
        with patch("app.application.tokens.auth_service", AuthService(auth_client)):
            result = await service.probe_account(self.db, self.account)
        self.assertTrue(result.success)
        self.assertEqual(result.credential_revision, 2)
        self.assertEqual(auth_client.refresh_access_token.await_count, 1)
        self.assertEqual(quota_client.fetch_quota.await_count, 2)
        snapshots = list((await self.db.execute(select(QuotaSnapshot).order_by(QuotaSnapshot.id))).scalars())
        self.assertEqual([s.http_status for s in snapshots], [401, 200])
        self.assertNotEqual(snapshots[0].check_id, snapshots[1].check_id)
        self.assertEqual(sum(s.request_count for s in snapshots), 2)
        self.assertEqual((await service.health_reader(self.db))(self.account, None)["health"]["code"], "healthy")
        self.assertEqual(len(list((await self.db.execute(select(Operation))).scalars())), 0)

    async def test_transport_does_not_fallback_immediately_after_429(self):
        client = ChatGPTClient()
        client._make_request = AsyncMock(return_value={"success": False, "status_code": 429, "retry_after": "300"})
        result = await client.refresh_access_token("r", "c", self.db, identifier="test")
        self.assertEqual(result["retry_after"], "300")
        self.assertEqual(client._make_request.await_count, 1)

    async def test_disabled_account_does_not_probe(self):
        self.account.operational_state = "disabled"
        await self.db.commit()
        client = AsyncMock()
        result = await QuotaService(client).probe_account(self.db, self.account)
        self.assertEqual(result.error_code, "account_disabled")
        client.fetch_quota.assert_not_called()


class QuotaMigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_snapshot_upgrade_keeps_unknown_evidence_and_disabled_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = create_async_engine(f"sqlite+aiosqlite:///{Path(tmp).as_posix()}/old.db")
            async with engine.begin() as conn:
                await conn.execute(text("""CREATE TABLE quota_snapshots (
                    id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, workspace_id INTEGER,
                    five_hour_used_percent INTEGER, five_hour_reset_at DATETIME,
                    seven_day_used_percent INTEGER, seven_day_reset_at DATETIME,
                    source VARCHAR(20) NOT NULL, queried_at DATETIME NOT NULL,
                    success BOOLEAN NOT NULL, error_code VARCHAR(40), error_message TEXT, created_at DATETIME
                )"""))
                await conn.execute(text("INSERT INTO quota_snapshots (id,account_id,source,queried_at,success,error_code) VALUES (1,1,'official','2026-09-01',0,'token_revoked')"))
            await bootstrap_schema(engine)
            await bootstrap_schema(engine)
            factory = async_sessionmaker(engine, expire_on_commit=False)
            async with factory() as db:
                snapshot = await db.get(QuotaSnapshot, 1)
                self.assertIsNone(snapshot.http_status)
                self.assertIsNone(snapshot.credential_revision)
                self.assertEqual(snapshot.error_code, "token_revoked")
                self.assertEqual((await db.get(SystemSetting, "official_quota_probe_enabled")).value, "false")
                version = (await db.execute(text("select sqlite_version()"))).scalar_one()
                self.assertGreaterEqual(tuple(map(int, version.split('.'))), (3,25,0))
            await engine.dispose()
