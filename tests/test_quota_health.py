import asyncio
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import AsyncMock

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker

from app.application.quota import QuotaService, context_key, snapshot_from_result
from app.core.crypto import token_cipher
from app.core.time import utcnow
from app.domain.quota import QuotaResult, parse_wham_usage
from app.domain.quota_health import present_context, result_state, retry_after
from app.integrations.openai.quota import OpenAIQuotaClient
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership
from app.persistence.models.quota import QuotaSnapshot, QuotaProbeState, ProbeDispatchLease
from app.persistence.models.operations import Operation


class HealthPolicyTests(unittest.TestCase):
    def test_classification_and_no_invented_http(self):
        for status, code, expected in [(401, "token_revoked", "auth_required"),
            (None, "token_revoked", "auth_required"), (403, None, "forbidden"),
            (429, None, "rate_limited"), (502, None, "temporary_failure"),
            (None, "transport", "temporary_failure"), (200, "parse_error", "parse_error"),
            (None, "missing_token", "unauthorized"), (None, "credential_error", "credential_error")]:
            with self.subTest(status=status, code=code):
                self.assertEqual(result_state(QuotaResult(False, http_status=status, error_code=code)), expected)
        self.assertEqual(result_state(QuotaResult(False, http_status=401, error_source="proxy")), "temporary_failure")

    def test_invalid_percent_is_not_success(self):
        for value in (None, "nan", "inf", "bad"):
            self.assertFalse(parse_wham_usage({"rate_limit": {"primary_window": {
                "limit_window_seconds": 18000, "used_percent": value}}}).success)

    def test_retry_after(self):
        now = utcnow()
        self.assertEqual(retry_after("7200", now), now + timedelta(hours=2))
        self.assertIsNone(retry_after("bad", now))
        self.assertIsNone(retry_after("-1", now))


class QuotaHealthIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = create_async_engine(f"sqlite+aiosqlite:///{Path(self.tmp.name).as_posix()}/test.db")
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = self.factory()
        self.account = Account(email="member@example.com", local_purpose="child", operational_state="active", auth_state="healthy",
                               access_token_encrypted=token_cipher().encrypt("test-token"))
        self.db.add(self.account)
        await self.db.commit()
        self.client = AsyncMock()
        self.service = QuotaService(self.client)
        self.now = utcnow()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()
        self.tmp.cleanup()

    async def probe(self, result, *, minutes=0, workspace_id=None):
        self.client.fetch_quota.return_value = result
        return await self.service.probe_account(self.db, self.account, workspace_id=workspace_id, now=self.now + timedelta(minutes=minutes))

    async def test_success_401_timeout_keeps_authoritative_failure_and_old_quota(self):
        await self.probe(QuotaResult(True, five_hour_used_percent=36, seven_day_used_percent=62, http_status=200))
        await self.probe(QuotaResult(False, http_status=401, error_code="token_revoked"), minutes=1)
        await self.probe(QuotaResult(False, error_code="transport"), minutes=2)
        health = (await self.service.health_reader(self.db))(self.account, None)
        self.assertEqual(health["health"]["label"], "401 · 授权失效")
        self.assertEqual(health["latest_check"]["state"], "temporary_failure")
        self.assertTrue(health["last_success_quota"]["stale"])
        self.assertEqual(health["quota"]["seven_day_used_percent"], 62)
        self.assertEqual(self.account.local_purpose, "child")
        self.assertEqual(self.account.operational_state, "active")

    async def test_revision_advances_for_direct_import_and_old_success_is_pending(self):
        await self.probe(QuotaResult(True, five_hour_used_percent=5, http_status=200))
        old = self.account.credential_revision
        self.account.access_token_encrypted = token_cipher().encrypt("new-token")
        await self.db.commit()
        self.assertEqual(self.account.credential_revision, old + 1)
        health = (await self.service.health_reader(self.db))(self.account, None)
        self.assertEqual(health["health"]["code"], "pending")
        self.assertTrue(health["quota"]["stale"])

    async def test_missing_and_undecryptable_are_different(self):
        self.account.access_token_encrypted = "not-valid-ciphertext"
        await self.db.commit()
        result = await self.probe(QuotaResult(True))
        self.assertEqual(result.error_code, "credential_error")
        self.client.fetch_quota.assert_not_called()
        self.account.access_token_encrypted = None
        await self.db.commit()
        result = await self.probe(QuotaResult(True), minutes=1)
        self.assertEqual(result.error_code, "missing_token")

    async def test_queue_is_persistent_deduplicated_and_read_only_until_worker(self):
        a = await self.service.enqueue(self.db, self.account, now=self.now)
        b = await self.service.enqueue(self.db, self.account, now=self.now)
        self.assertEqual(a["operation_id"], b["operation_id"])
        self.client.fetch_quota.assert_not_called()
        self.assertEqual(len(list((await self.db.execute(select(QuotaProbeState))).scalars())), 1)
        self.client.fetch_quota.return_value = QuotaResult(True, five_hour_used_percent=0)
        await self.service.run_queued_once(self.db, now=self.now)
        op = (await self.db.execute(select(Operation))).scalar_one()
        self.assertEqual(op.state, "success")
        self.assertEqual(self.client.fetch_quota.await_count, 1)

    async def test_retry_after_is_not_bypassed_by_manual_queue(self):
        deadline = self.now + timedelta(hours=3)
        await self.probe(QuotaResult(False, http_status=429, retry_after_at=deadline))
        await self.service.enqueue(self.db, self.account, now=self.now + timedelta(minutes=1))
        state = await self.db.get(QuotaProbeState, context_key(self.account.id, None), populate_existing=True)
        from app.core.time import as_utc
        self.assertEqual(as_utc(state.next_check_at), deadline)
        self.client.fetch_quota.reset_mock()
        await self.service.run_queued_once(self.db, now=self.now + timedelta(minutes=2))
        self.client.fetch_quota.assert_not_called()

    async def test_old_credential_response_does_not_override_new_credentials(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def fetch(**kwargs):
            started.set()
            await release.wait()
            return QuotaResult(False, http_status=401)
        self.client.fetch_quota.side_effect = fetch
        work = asyncio.create_task(self.service.probe_account(self.db, self.account, now=self.now))
        await started.wait()
        async with self.factory() as other:
            account = await other.get(Account, self.account.id)
            account.access_token_encrypted = token_cipher().encrypt("replacement")
            await other.commit()
        release.set()
        result = await work
        self.assertEqual(result.error_code, "superseded")
        snap = (await self.db.execute(select(QuotaSnapshot))).scalar_one()
        self.assertFalse(snap.accepted)

    async def test_same_revision_late_result_cannot_replace_newer_check(self):
        started, release = asyncio.Event(), asyncio.Event()
        async def fetch(**kwargs):
            started.set()
            await release.wait()
            return QuotaResult(False, http_status=401)
        self.client.fetch_quota.side_effect = fetch
        work = asyncio.create_task(self.service.probe_account(self.db, self.account, now=self.now))
        await started.wait()
        async with self.factory() as other:
            account = await other.get(Account, self.account.id)
            newer = QuotaService(AsyncMock(fetch_quota=AsyncMock(return_value=QuotaResult(True, five_hour_used_percent=10))))
            await newer.probe_account(other, account, now=self.now + timedelta(minutes=7))
        release.set()
        await work
        latest = await self.service.latest_official_by_contexts(self.db)
        self.assertTrue(latest[(self.account.id, None)].success)

    async def test_context_isolation_and_local_owner_role(self):
        workspaces = [Workspace(name=f"Team {i}", official_workspace_id=f"00000000-0000-0000-0000-00000000000{i}") for i in (1, 2)]
        self.db.add_all(workspaces)
        await self.db.flush()
        for ws in workspaces:
            self.db.add(WorkspaceMembership(account_id=self.account.id, workspace_id=ws.id, membership_state="joined", official_role="owner", local_purpose="child"))
        await self.db.commit()
        await self.probe(QuotaResult(True, five_hour_used_percent=24), workspace_id=workspaces[0].id)
        await self.probe(QuotaResult(False, http_status=401), workspace_id=workspaces[1].id, minutes=1)
        read = await self.service.health_reader(self.db)
        self.assertEqual(read(self.account, workspaces[0].id)["health"]["code"], "healthy")
        self.assertIsNone(read(self.account, workspaces[1].id)["last_success_quota"])
        from app.application.queries.portfolio import portfolio_query
        portfolio = await portfolio_query(self.db)
        self.assertEqual(portfolio["summary"]["accounts"], 1)
        self.assertEqual(portfolio["accounts"][0]["health"]["code"], "partial")
        self.assertTrue(all(g["mother"] is None for g in portfolio["groups"]))

    async def test_history_keeps_unknown_version_and_never_forges_401(self):
        snap = snapshot_from_result(self.account.id, QuotaResult(False, error_code="token_revoked"), self.now)
        self.db.add(snap)
        await self.db.commit()
        health = (await self.service.health_reader(self.db))(self.account, None)
        self.assertIsNone(health["latest_check"]["http_status"])
        self.assertFalse(health["latest_check"]["current_credential"])


class TransportEvidenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_status_and_safe_message_survive_business_code(self):
        transport = AsyncMock()
        transport.fetch.return_value = {"success": False, "status_code": 401, "error_code": "token_revoked", "error": "Bearer SECRET"}
        result = await OpenAIQuotaClient(transport).fetch_quota(access_token="secret", db_session=None)
        self.assertEqual(result.http_status, 401)
        self.assertNotIn("SECRET", result.error_message)
