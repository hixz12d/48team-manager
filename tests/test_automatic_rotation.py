"""Night rotation regression tests. SQLite and mocked providers only."""
import asyncio
import json
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application import automatic_rotation as automatic
from app.application.operations import operation_store
from app.application.rotate import RotateService
from app.application.settings import upsert_setting
from app.core.config import Settings
from app.core.time import utcnow
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership
from app.persistence.models.operations import Operation
from tests.helpers import make_client
from tests.test_rotate import _FakeSub2Api


class AutomaticRotationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.now = utcnow()
        self.cfg = {"auto_rotate_enabled": True, "auto_rotate_daily_limit": 2, "auto_rotate_scope": "all", "auto_rotate_workspace_ids": []}

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def seed(self, suffix="1"):
        owner = Account(email=f"owner{suffix}@example.com", local_purpose="mother", operational_state="active")
        child = Account(email=f"child{suffix}@example.com", local_purpose="child", operational_state="active",
                        auth_state="healthy", credential_revision=1, last_reauth_code="account_deactivated")
        self.db.add_all([owner, child]); await self.db.flush()
        ws = Workspace(owner_account_id=owner.id, official_workspace_id=str(uuid4()), status="active", seat_limit=3)
        self.db.add(ws); await self.db.flush()
        self.db.add(WorkspaceMembership(workspace_id=ws.id, account_id=child.id, membership_state="joined", official_role="owner", local_purpose="child"))
        self.db.add(ExternalBinding(provider="sub2api", workspace_id=ws.id, local_account_id=child.id,
                                    remote_account_id=suffix, binding_state="verified"))
        await self.db.commit()
        remote = {"id": int(suffix), "credentials": {"email": child.email}}
        return child, ws, remote

    async def test_disabled_does_not_read_or_write_provider(self):
        sub = AsyncMock()
        result = await RotateService(sub2api=sub).run_once(self.db, settings={"auto_rotate_enabled": False})
        self.assertFalse(result["enabled"])
        sub.list_status_accounts.assert_not_called()

    async def test_saved_switch_overrides_false_environment_and_zero_limit_survives(self):
        await upsert_setting(self.db, "auto_rotate_enabled", "true")
        await upsert_setting(self.db, "auto_rotate_daily_limit", "0")
        with patch("app.application.rotate.load_settings", return_value=Settings(_env_file=None, auto_rotate_enabled=False)):
            cfg = await RotateService().load_settings(self.db)
        self.assertTrue(cfg["auto_rotate_enabled"])
        self.assertEqual(cfg["auto_rotate_daily_limit"], 0)

    async def test_weekly_confirmation_uses_new_probe_and_rejects_failed_new_probe(self):
        child, ws, _ = await self.seed()
        quota = AsyncMock()
        quota.latest_official.return_value = SimpleNamespace(success=True, seven_day_used_percent=100)
        quota.probe_account.return_value = SimpleNamespace(success=False)
        result = await RotateService(quota=quota)._confirm_weekly_limit(self.db, account=child, remote_id=1, workspace_id=ws.id)
        self.assertFalse(result["ok"])
        quota.latest_official.assert_not_awaited()
        quota.probe_account.assert_awaited_once_with(self.db, child, workspace_id=ws.id)

    async def test_first_capped_team_does_not_starve_second(self):
        first, ws1, r1 = await self.seed("1")
        second, ws2, r2 = await self.seed("2")
        for _ in range(2):
            op = await operation_store.create(self.db, op_type="rotate", workspace_id=ws1.id, source="auto", now=self.now)
            await operation_store.finish(self.db, op, {"success": True}, now=self.now)
        await self.db.commit()
        service = RotateService(sub2api=_FakeSub2Api([r1, r2]))
        service.run_rotate_saga = AsyncMock(return_value={"success": True})
        result = await service.run_once(self.db, now=self.now, settings=self.cfg, in_test=True)
        self.assertEqual(result["rotated"], 1)
        self.assertEqual(result["capped"], 1)
        self.assertEqual(service.run_rotate_saga.await_args.kwargs["workspace_id"], ws2.id)

    async def test_kicked_partial_counts_against_daily_budget(self):
        child, ws, _ = await self.seed()
        op = await operation_store.create(self.db, op_type="rotate", workspace_id=ws.id, source="auto", now=self.now)
        await operation_store.mark_step(self.db, op, "kicked", state="success")
        await operation_store.finish(self.db, op, {"success": False, "partial": True}, now=self.now)
        await self.db.commit()
        self.assertEqual(await RotateService().count_today_auto_rotates(self.db, ws.id, self.now), 1)

    async def test_partial_workspace_blocks_repeated_rotation(self):
        child, ws, remote = await self.seed()
        op = await operation_store.create(self.db, op_type="rotate", workspace_id=ws.id, source="auto")
        await operation_store.finish(self.db, op, {"success": False, "partial": True, "error_code": "oauth_failed"})
        await self.db.commit()
        service = RotateService(sub2api=_FakeSub2Api([remote]))
        service.run_rotate_saga = AsyncMock()
        await service.run_once(self.db, settings=self.cfg, in_test=True)
        service.run_rotate_saga.assert_not_awaited()

    async def test_failure_after_kick_preserves_child_and_error(self):
        child, ws, _ = await self.seed()
        service = RotateService()
        service.kick_to_standby = AsyncMock(return_value={"success": True, "vacancy": {"safe_to_refill": True}})
        refill = AsyncMock(return_value={"success": False, "partial": True, "authorized": True,
                                        "error_code": "auto_publish_pending", "child": {"id": 99}})
        result = await service.kick_and_refill(self.db, workspace_id=ws.id, email=child.email, force_refill=True, refill=refill)
        self.assertTrue(result["partial"])
        self.assertEqual(result["error_code"], "auto_publish_pending")
        self.assertEqual(result["invite"]["child"]["id"], 99)

    async def test_default_automatic_refill_requires_oauth_and_pool_then_publish(self):
        service = SimpleNamespace(onboard=AsyncMock())
        service.onboard.refill.return_value = {"success": True, "authorized": True, "child": {"id": 3}}
        with patch.object(automatic, "publish_replacement", new=AsyncMock(return_value={"success": True})) as publish:
            result = await automatic.refill_and_publish(service, self.db, workspace_id=7, seat_intent="premium", role="owner")
        self.assertTrue(result["success"])
        args = service.onboard.refill.await_args.kwargs
        self.assertTrue(args["oauth_signup"])
        self.assertTrue(args["use_phone_pool"])
        self.assertEqual(args["seat_intent"], "premium")
        publish.assert_awaited_once()

    async def test_oauth_failure_never_publishes(self):
        service = SimpleNamespace(onboard=AsyncMock())
        service.onboard.refill.return_value = {"success": False, "partial": True, "error_code": "phone_verification_required"}
        with patch.object(automatic, "publish_replacement", new=AsyncMock()) as publish:
            result = await automatic.refill_and_publish(service, self.db, workspace_id=7)
        self.assertTrue(result["partial"])
        publish.assert_not_awaited()

    async def test_publish_success_requires_fresh_healthy_readback(self):
        child, ws, _ = await self.seed()
        invite = {"success": True, "authorized": True, "child": {"id": child.id}}
        for state, stale, expected in [("healthy", False, True), ("healthy", True, False), ("paused", False, False), ("unknown", False, False)]:
            with self.subTest(state=state, stale=stale), \
                 patch.object(automatic, "account_sub2api_push", new=AsyncMock(return_value={"ok": True, "operation_id": "write"})), \
                 patch("app.application.sub2api_status.refresh", new=AsyncMock()), \
                 patch("app.application.sub2api_status.payloads", new=AsyncMock(return_value=({(child.id, ws.id): {"state": state, "stale": stale}}, {}))):
                result = await automatic.publish_replacement(self.db, invite, ws.id)
            self.assertEqual(result["success"], expected)
            if not expected:
                self.assertEqual(result["error_code"], "auto_publish_pending")
                self.assertTrue(result["publish_written"])

    async def test_written_credentials_are_not_resubmitted_while_waiting_for_status(self):
        child, ws, _ = await self.seed()
        invite = {"authorized": True, "publish_written": True, "publish_receipt_ok": True, "child": {"id": child.id}}
        with patch.object(automatic, "account_sub2api_push", new=AsyncMock()) as push, \
             patch("app.application.sub2api_status.refresh", new=AsyncMock()), \
             patch("app.application.sub2api_status.payloads", new=AsyncMock(return_value=({(child.id, ws.id): {"state": "healthy", "stale": False}}, {}))):
            self.assertTrue((await automatic.publish_replacement(self.db, invite, ws.id))["success"])
        push.assert_not_awaited()

    async def pending(self):
        child, ws, _ = await self.seed()
        op = await operation_store.create(self.db, op_type="rotate", workspace_id=ws.id, source="auto", account_id=child.id)
        invite = {"child": {"id": child.id}, "authorized": True, "publish_revision": 1,
                  "publish_attempts": 1, "publish_retry_at": (self.now - timedelta(seconds=1)).isoformat()}
        await operation_store.finish(self.db, op, {"success": False, "partial": True,
                                                  "error_code": "auto_publish_pending", "invite": invite})
        await self.db.commit()
        return child, ws, op

    async def test_sync_retry_reuses_replacement_and_finishes_original_rotation(self):
        child, ws, parent = await self.pending()
        with patch.object(automatic, "publish_replacement", new=AsyncMock(return_value={"success": True, "authorized": True, "child": {"id": child.id}})) as publish:
            result = await automatic.retry_pending_publish(self.db, now=self.now, settings=self.cfg)
        self.assertTrue(result["retried"])
        self.assertEqual(parent.state, "success")
        self.assertEqual(publish.await_args.args[1]["child"]["id"], child.id)
        self.assertEqual(publish.await_args.args[2], ws.id)

    async def test_sync_retry_waits_and_preserves_pending_work_across_sessions(self):
        _, _, parent = await self.pending()
        parent_id = parent.id
        with patch.object(automatic, "publish_replacement", new=AsyncMock(return_value={"success": False, "error_code": "auto_publish_pending", "publish_pending": True})) as publish:
            await automatic.retry_pending_publish(self.db, now=self.now, settings=self.cfg)
            await automatic.retry_pending_publish(self.db, now=self.now, settings=self.cfg)
        self.assertEqual(publish.await_count, 1)
        await self.db.close()
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        saved = await self.db.get(Operation, parent_id)
        self.assertEqual(saved.state, "partial")
        self.assertEqual(json.loads(saved.result_json)["invite"]["publish_attempts"], 2)

    async def test_changed_credential_stops_sync_retry(self):
        child, _, parent = await self.pending()
        child.credential_revision = 2
        await self.db.commit()
        with patch.object(automatic, "publish_replacement", new=AsyncMock()) as publish:
            await automatic.retry_pending_publish(self.db, now=self.now, settings=self.cfg)
        publish.assert_not_awaited()
        self.assertEqual(parent.state, "manual_required")

    async def test_workspace_lock_prevents_sync_retry(self):
        _, ws, _ = await self.pending()
        await operation_store.create_workspace_locked(self.db, op_type="onboard", workspace_id=ws.id)
        await self.db.commit()
        with patch.object(automatic, "publish_replacement", new=AsyncMock()) as publish:
            await automatic.retry_pending_publish(self.db, now=self.now, settings=self.cfg)
        publish.assert_not_awaited()

    async def test_retry_stops_at_budget(self):
        _, _, parent = await self.pending()
        result = json.loads(parent.result_json)
        result["invite"]["publish_attempts"] = automatic.MAX_PUBLISH_ATTEMPTS
        parent.result_json = json.dumps(result)
        await self.db.commit()
        with patch.object(automatic, "publish_replacement", new=AsyncMock()) as publish:
            await automatic.retry_pending_publish(self.db, now=self.now, settings=self.cfg)
        publish.assert_not_awaited()
        self.assertEqual(parent.error_code, "auto_publish_review")


    async def test_pool_phone_is_reserved_only_for_automatic_oauth_and_released_unused(self):
        from app.application.oauth_signup import run_invited_oauth_signup
        from app.application.resources.phones import phone_pool_service
        from app.persistence.models.resources import PhonePool
        child, ws, _ = await self.seed()
        op = await operation_store.create(self.db, op_type="rotate", workspace_id=ws.id, source="auto")
        await phone_pool_service.import_lines(self.db, "+15555555555----https://sms.example/receipt?key=private")
        with patch("app.application.oauth_signup.browser_slot.run_reauth_isolated", new=AsyncMock(return_value={"ok": False, "error_code": "browser_failed"})) as runner:
            result = await run_invited_oauth_signup(self.db, child=child, workspace=ws, password="fixture",
                pickup_url="", use_cloudflare=True, cf_config={"base_url": "https://mail.example", "address": "inbox@example.com", "admin_password": "fixture"},
                job_id=op.public_id, use_phone_pool=True)
        self.assertFalse(result["ok"])
        self.assertTrue(runner.await_args.kwargs["allow_sms"])
        self.assertEqual(runner.await_args.kwargs["max_sms_submissions"], 1)
        phone = await self.db.scalar(select(PhonePool))
        self.assertIsNone(phone.reserved_by)
        self.assertEqual(phone.used_count, 0)
        self.assertNotIn("private", json.dumps(result))

    async def test_successful_sms_counts_even_if_oauth_later_fails(self):
        from app.application.oauth_signup import run_invited_oauth_signup
        from app.application.resources.phones import phone_pool_service
        from app.persistence.models.resources import PhonePool
        child, ws, _ = await self.seed()
        op = await operation_store.create(self.db, op_type="rotate", workspace_id=ws.id, source="auto")
        await phone_pool_service.import_lines(self.db, "+15555555555----https://sms.example/receipt?key=private")
        with patch("app.application.oauth_signup.browser_slot.run_reauth_isolated", new=AsyncMock(return_value={"ok": False, "error_code": "callback_missing", "sms_verified": True})):
            await run_invited_oauth_signup(self.db, child=child, workspace=ws, password="fixture",
                pickup_url="", use_cloudflare=True, cf_config={"base_url": "https://mail.example", "address": "inbox@example.com", "admin_password": "fixture"},
                job_id=op.public_id, use_phone_pool=True)
        phone = await self.db.scalar(select(PhonePool))
        self.assertEqual(phone.used_count, 1)
        self.assertIsNone(phone.reserved_by)


    async def test_real_automatic_entry_wires_inherited_seat_oauth_and_publish(self):
        child, ws, remote = await self.seed()
        onboard = AsyncMock()
        onboard.refill.return_value = {"success": True, "authorized": True, "child": {"id": child.id}}
        service = RotateService(sub2api=_FakeSub2Api([remote]), onboard=onboard)
        service._pause_and_drain = AsyncMock(return_value={"ok": True})
        from app.domain.vacancy import parse_policy_notice
        vacancy = parse_policy_notice({"policy_notice": {"vacancy_ordinal": 1, "free_vacancy_threshold": 5}})
        service.kick_to_standby = AsyncMock(return_value={"success": True, "vacancy": vacancy})
        with patch.object(automatic, "preflight", new=AsyncMock(return_value={"role": "owner", "seat_intent": "premium"})), \
             patch.object(automatic, "publish_replacement", new=AsyncMock(return_value={"success": True, "authorized": True, "pushed": True, "child": {"id": child.id}})) as publish, \
             patch.object(automatic, "refresh_after_rotation", new=AsyncMock()):
            result = await service.run_once(self.db, settings=self.cfg)
        self.assertEqual(result["rotated"], 1)
        self.assertEqual(onboard.refill.await_args.kwargs["seat_intent"], "premium")
        self.assertTrue(onboard.refill.await_args.kwargs["oauth_signup"])
        self.assertTrue(onboard.refill.await_args.kwargs["use_phone_pool"])
        publish.assert_awaited_once()
        op = await self.db.scalar(select(Operation).where(Operation.source == "auto"))
        self.assertEqual(op.state, "success")
        self.assertEqual(op.idempotency_key, f"ws-mutation:{ws.id}")

    async def test_preflight_failure_never_pauses_or_kicks_and_remains_retryable(self):
        _, _, remote = await self.seed()
        service = RotateService(sub2api=_FakeSub2Api([remote]))
        service.run_rotate_saga = AsyncMock()
        with patch.object(automatic, "preflight", new=AsyncMock(side_effect=ValueError("resource unavailable"))), \
             patch.object(automatic, "refresh_after_rotation", new=AsyncMock()):
            await service.run_once(self.db, settings=self.cfg)
        service.run_rotate_saga.assert_not_awaited()
        op = await self.db.scalar(select(Operation))
        self.assertEqual(op.state, "failed")
        self.assertEqual(op.error_code, "rotation_preflight_failed")

    async def test_preflight_preserves_live_owner_premium_and_rejects_unknown_seat(self):
        child, ws, _ = await self.seed()
        owner = await self.db.get(Account, ws.owner_account_id)
        owner.proxy, owner.access_token_encrypted = "socks5h://fixture.invalid:1080", "sealed"
        service = SimpleNamespace(sub2api=AsyncMock(), workspaces=AsyncMock(), _remote_binding_for=AsyncMock(return_value={"state": "matched"}))
        service.sub2api.load_config.return_value = {"configured": True}
        service.workspaces.lookup_live_member.return_value = ({"success": True}, {"status": "joined", "role": "account-owner", "seat_type": "prolite"})
        with patch("app.application.reauth.load_cf_config", new=AsyncMock(return_value={"key": "fixture"})), \
             patch("app.application.resources.hme.load_config", new=AsyncMock(return_value=SimpleNamespace(configured=True))), \
             patch("app.application.sub2api_defaults.load_defaults", new=AsyncMock()), \
             patch("app.application.sub2api_defaults.validate_defaults", new=AsyncMock()), \
             patch("app.core.config.load_settings", return_value=Settings(_env_file=None)):
            self.assertEqual(await automatic.preflight(service, self.db, ws, child), {"role": "owner", "seat_intent": "premium"})
            service.workspaces.lookup_live_member.return_value[1]["seat_type"] = "unknown"
            with self.assertRaises(ValueError):
                await automatic.preflight(service, self.db, ws, child)


class RotationSettingsTests(unittest.TestCase):
    def test_switch_persists_and_settings_are_authenticated(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp), auto_rotate_enabled=False) as client:
            self.assertEqual(client.patch("/api/settings", json={"automation": {"auto_rotate": True}}).status_code, 401)
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            result = client.patch("/api/settings", json={"automation": {"auto_rotate": True, "auto_rotate_daily_limit": 8, "auto_rotate_scope": "all"}})
            self.assertEqual(result.status_code, 200)
            self.assertTrue(client.get("/api/settings").json()["automation"]["auto_rotate"]["auto_rotate_enabled"])
            state = client.get("/api/runtime/status").json()["auto_rotation"]
            self.assertTrue(state["enabled"])
            self.assertEqual(state["daily_limit"], 8)
            for invalid in (-1, 51):
                self.assertEqual(client.patch("/api/settings", json={"automation": {"auto_rotate_daily_limit": invalid}}).status_code, 422)
            client.patch("/api/settings", json={"automation": {"auto_rotate": False}})
            self.assertFalse(client.get("/api/runtime/status").json()["auto_rotation"]["enabled"])

    def test_scheduler_keeps_rotation_serial_and_status_frequent(self):
        from app.application.jobs import scheduler as jobs
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        with patch.object(jobs, "scheduler", AsyncIOScheduler()):
            jobs.configure_jobs(Settings(_env_file=None))
            self.assertEqual(jobs.scheduler.get_job("auto_rotate_scan").trigger.interval.total_seconds(), 60)
            self.assertEqual(jobs.scheduler.get_job("auto_rotate_scan").max_instances, 1)
            self.assertEqual(jobs.scheduler.get_job("sub2api_status_sync").trigger.interval.total_seconds(), 15)
