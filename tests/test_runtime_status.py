import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.jobs import scheduler as scheduling
from app.application.operations import operation_store
from app.application.queries.runtime_status import runtime_operation, runtime_status
from app.core.time import utcnow
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.settings import SystemSetting
from tests.helpers import make_client


class RuntimeStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        await bootstrap_schema(self.engine)
        self.factory = async_sessionmaker(self.engine, expire_on_commit=False)
        self.db = self.factory()
        for key in ("auto_reauth_enabled", "auto_rotate_enabled", "official_quota_probe_enabled"):
            self.db.add(SystemSetting(key=key, value="false"))
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def test_manual_reauth_is_independent_from_policy_and_secrets_are_excluded(self):
        row = await operation_store.create(self.db, op_type="reauth", account_id=1, source="manual",
                                           input_payload={"ticket": "secret-ticket"})
        row.current_step = "https://secret.invalid?token=secret-stage"
        row.error_code = "secret-code"
        row.error_message = "secret-message"
        row.log_json = '[{"message":"secret-log"}]'
        await self.db.commit()
        with patch("app.integrations.sub2api.client.sub2api_client.get_account", new_callable=AsyncMock) as remote:
            payload = await runtime_status(self.db)
            remote.assert_not_called()
        self.assertEqual(payload["counts"]["running"], 1)
        reauth = next(p for p in payload["policies"] if p["id"] == "auto_reauth")
        self.assertFalse(reauth["enabled"])
        self.assertEqual(payload["active_operations"][0]["trigger_source"], "manual")
        self.assertNotIn("secret-", json.dumps(payload))
        self.assertNotIn("input_json", json.dumps(payload))

    async def test_counts_are_not_truncated_by_recent_history(self):
        await operation_store.create(self.db, op_type="workspace_sync", state="queued")
        await operation_store.create(self.db, op_type="reauth", state="manual_required")
        for _ in range(55):
            row = await operation_store.create(self.db, op_type="auth_probe")
            await operation_store.finish(self.db, row, {"success": True})
        await self.db.commit()
        payload = await runtime_status(self.db)
        self.assertEqual(payload["counts"], {"running": 0, "queued": 1, "waiting": 0, "waiting_user": 1})
        self.assertEqual(len(payload["recent_operations"]), 5)
        self.assertEqual(payload["active_total"], 2)

    async def test_no_heartbeat_is_not_healthy_and_stale_is_reported(self):
        now = utcnow()
        fake = SimpleNamespace(running=True, get_job=lambda _: None)
        with patch.object(scheduling, "scheduler", fake), patch.object(scheduling, "last_heartbeat_at", None):
            payload = await runtime_status(self.db, now=now)
            self.assertEqual(payload["runner"]["state"], "unknown")
            auth = next(p for p in payload["policies"] if p["id"] == "token_refresh")
            self.assertEqual(auth["state"], "not_ready")
            self.assertIsNone(auth["next_run_at"])
        with patch.object(scheduling, "scheduler", fake), patch.object(scheduling, "last_heartbeat_at", now - timedelta(seconds=20)):
            self.assertEqual((await runtime_status(self.db, now=now))["runner"]["state"], "stale")
        with patch.object(scheduling, "scheduler", fake), patch.object(scheduling, "last_heartbeat_at", now):
            self.assertEqual((await runtime_status(self.db, now=now))["runner"]["state"], "healthy")

    async def test_real_registered_next_scan_time_and_no_invented_member_schedule(self):
        now = utcnow()
        fake = SimpleNamespace(running=True, get_job=lambda _: SimpleNamespace(next_run_time=now))
        with patch.object(scheduling, "scheduler", fake):
            payload = await runtime_status(self.db)
        auth = next(p for p in payload["policies"] if p["id"] == "token_refresh")
        self.assertEqual(auth["next_run_at"], now.isoformat())
        self.assertFalse(any(p["id"] == "workspace_sync" for p in payload["policies"]))

    async def test_wait_reasons_have_distinct_evidence(self):
        now = utcnow()
        row = await operation_store.create(self.db, op_type="quota_probe", state="queued")
        payload = runtime_operation(row, now=now, next_retry_at=now + timedelta(minutes=1))
        self.assertEqual(payload["status"], "waiting_retry")
        row.state = "manual_required"
        self.assertEqual(runtime_operation(row)["status"], "waiting_user")
        row.op_type = "reauth"
        row.state = "queued"
        with patch("app.application.queries.runtime_status.browser.lock", return_value=SimpleNamespace(locked=lambda: True)):
            self.assertEqual(runtime_operation(row)["status"], "waiting_browser")


class RuntimeStatusApiTests(unittest.TestCase):
    def test_read_requires_auth_and_does_not_run_external_work(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            self.assertEqual(client.get("/api/runtime/status").status_code, 401)
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            with patch("app.application.tokens.auth_service.refresh_account", new_callable=AsyncMock) as refresh:
                response = client.get("/api/runtime/status")
                self.assertEqual(response.status_code, 200)
                refresh.assert_not_called()
            self.assertEqual(response.json()["runner"]["state"], "unavailable")
            self.assertTrue(response.json()["generated_at"].endswith("+00:00"))
