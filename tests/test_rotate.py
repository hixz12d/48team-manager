import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.operations import operation_store
from app.application.rotate import RotateService
from app.application.tokens import encrypt_secret
from app.domain.quota import QuotaResult, SOURCE_OFFICIAL
from app.domain.rotate import (
    DEFAULT_AUTO_ROTATE_DAILY_LIMIT,
    DEFAULT_AUTO_ROTATE_ENABLED,
    DEFAULT_AUTO_ROTATE_FORCE_REFILL,
    classify_rotate_reason,
    daily_auto_rotate_limit_reached,
    official_weekly_limit_full,
    official_weekly_reset_at,
)
from app.domain.vacancy import parse_policy_notice
from app.persistence.database import Base
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership


WORKSPACE_UUID = "11111111-1111-1111-1111-111111111111"


class RotatePolicyTests(unittest.TestCase):
    def test_layer_three_defaults_are_off(self):
        self.assertFalse(DEFAULT_AUTO_ROTATE_ENABLED)
        self.assertFalse(DEFAULT_AUTO_ROTATE_FORCE_REFILL)
        self.assertEqual(DEFAULT_AUTO_ROTATE_DAILY_LIMIT, 2)
        self.assertTrue(daily_auto_rotate_limit_reached(2))
        self.assertFalse(daily_auto_rotate_limit_reached(1))

    def test_rotate_queue_skips_five_hour_and_token_401(self):
        self.assertIsNone(classify_rotate_reason(kind="5h"))
        self.assertIsNone(classify_rotate_reason(kind="401"))
        self.assertEqual(
            classify_rotate_reason(kind="401", last_reauth_code="account_deactivated"),
            "deactivated",
        )
        self.assertEqual(classify_rotate_reason(kind="429"), "weekly_limit")
        self.assertIsNone(classify_rotate_reason(kind="429", on_weekly_limit=False))

    def test_official_weekly_limit_requires_full_seven_day(self):
        self.assertTrue(official_weekly_limit_full({"seven_day": {"utilization": 100}}))
        self.assertTrue(official_weekly_limit_full({"seven_day": {"utilization": 100.4}}))
        self.assertFalse(official_weekly_limit_full({"seven_day": {"utilization": 0}}))
        self.assertFalse(official_weekly_limit_full({"seven_day": {"utilization": 96}}))
        self.assertIsNone(official_weekly_limit_full({}))
        self.assertIsNone(official_weekly_limit_full(None))
        self.assertTrue(
            official_weekly_limit_full(
                QuotaResult(success=True, source=SOURCE_OFFICIAL, seven_day_used_percent=100)
            )
        )
        self.assertFalse(
            official_weekly_limit_full(
                QuotaResult(success=True, source=SOURCE_OFFICIAL, seven_day_used_percent=0)
            )
        )

    def test_official_weekly_reset_at_keeps_utc(self):
        when = official_weekly_reset_at({
            "seven_day": {"utilization": 100, "resets_at": "2026-09-04T12:37:02+08:00"},
        })
        self.assertEqual(when, datetime(2026, 9, 4, 4, 37, 2, tzinfo=timezone.utc))


class _FakeSub2Api:
    def __init__(self, accounts=None, usage=None):
        self.accounts = accounts or []
        self.usage = usage
        self.paused = []
        self.deleted = []

    def schedule_kind(self, account):
        extra = account.get("extra") or {}
        if str(account.get("error_message") or "").lower().find("401") >= 0:
            return {"kind": "401", "label": "401"}
        if int(extra.get("codex_7d_used_percent") or 0) >= 100:
            return {"kind": "429", "label": "429"}
        if int(extra.get("codex_5h_used_percent") or 0) >= 100:
            return {"kind": "5h", "label": "5h"}
        return {"kind": "ok", "label": "ok"}

    def account_email(self, account):
        creds = account.get("credentials") or {}
        return str(creds.get("email") or "").strip().lower()

    async def list_status_accounts(self, db):
        return list(self.accounts)

    async def fetch_account_usage(self, db, account_id, source="active", force=True):
        self.last_fetch = {"account_id": account_id, "force": force, "source": source}
        if isinstance(self.usage, Exception):
            raise self.usage
        return self.usage or {}

    async def set_account_schedulable(self, db, account_id, schedulable):
        self.paused.append((account_id, schedulable))
        return {"patched": True, "account": {"schedulable": schedulable}}

    async def delete_accounts(self, db, account_ids):
        self.deleted.extend(account_ids)
        return {"deleted": list(account_ids), "failed": []}


class _FakeQuota:
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.probed = 0

    async def latest_official(self, db, account_id):
        return self.snapshot

    async def probe_account(self, db, account):
        self.probed += 1
        return self.snapshot


class RotateSagaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _seed_child(self, *, email="full@icloud.com", remote_id="88", last_reauth=""):
        owner = Account(
            email="owner@example.com",
            official_plan="unknown",
            local_purpose="mother",
            operational_state="active",
            access_token_encrypted=encrypt_secret("tok"),
        )
        child = Account(
            email=email,
            official_plan="unknown",
            local_purpose="child",
            operational_state="active",
            last_reauth_code=last_reauth or None,
        )
        self.session.add_all([owner, child])
        await self.session.flush()
        workspace = Workspace(
            official_workspace_id=WORKSPACE_UUID,
            owner_account_id=owner.id,
            status="active",
            seat_limit=5,
        )
        self.session.add(workspace)
        await self.session.flush()
        self.session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                account_id=child.id,
                official_role="member",
                membership_state="joined",
                local_purpose="child",
            )
        )
        self.session.add(
            ExternalBinding(
                provider="sub2api",
                local_account_id=child.id,
                remote_account_id=str(remote_id),
                binding_state="verified",
            )
        )
        await self.session.commit()
        return owner, child, workspace

    def _settings(self, **overrides):
        payload = {
            "auto_rotate_enabled": True,
            "auto_rotate_on_deactivated": True,
            "auto_rotate_on_weekly_limit": True,
            "auto_rotate_force_refill": False,
            "auto_rotate_daily_limit": 2,
        }
        payload.update(overrides)
        return payload

    async def test_disabled_by_default_does_not_rotate(self):
        service = RotateService(sub2api=_FakeSub2Api())
        stats = await service.run_once(self.session, settings=self._settings(auto_rotate_enabled=False))
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["rotated"], 0)

    async def test_weekly_limit_skips_kick_when_official_usage_is_zero(self):
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        _owner, child, _workspace = await self._seed_child()
        remote = {
            "id": 88,
            "credentials": {"email": child.email},
            "extra": {"codex_7d_used_percent": 100},
        }
        snapshot = type("Snap", (), {
            "success": True,
            "seven_day_used_percent": 0,
            "seven_day_reset_at": now + timedelta(days=4),
        })()
        service = RotateService(sub2api=_FakeSub2Api([remote]), quota=_FakeQuota(snapshot))
        stats = await service.run_once(
            self.session,
            now=now,
            settings=self._settings(),
            accounts=[remote],
            in_test=True,
        )
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["rotated"], 0)
        self.assertEqual(stats["email"], child.email)
        refreshed = await self.session.get(Account, child.id)
        self.assertEqual(refreshed.operational_state, "active")

    async def test_weekly_limit_kicks_after_official_usage_still_full(self):
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        _owner, child, _workspace = await self._seed_child()
        remote = {
            "id": 88,
            "credentials": {"email": child.email},
            "extra": {"codex_7d_used_percent": 100},
        }
        snapshot = type("Snap", (), {
            "success": True,
            "seven_day_used_percent": 100,
            "seven_day_reset_at": now + timedelta(days=4),
        })()
        vacancy = parse_policy_notice({"policy_notice": {"vacancy_ordinal": 1, "free_vacancy_threshold": 5}})
        service = RotateService(sub2api=_FakeSub2Api([remote]), quota=_FakeQuota(snapshot))
        service.kick_to_standby = AsyncMock(return_value={"success": True, "status": "standby", "vacancy": vacancy})
        refill = AsyncMock(return_value={"success": True, "child": {"email": "new@icloud.com"}})
        stats = await service.run_once(
            self.session,
            now=now,
            settings=self._settings(),
            accounts=[remote],
            refill=refill,
            in_test=True,
        )
        self.assertEqual(stats["rotated"], 1)
        self.assertEqual(stats["reason"], "weekly_limit")
        service.kick_to_standby.assert_awaited_once()
        refill.assert_awaited_once()

    async def test_five_hour_full_does_not_enter_rotate_queue(self):
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        _owner, child, _workspace = await self._seed_child()
        remote = {
            "id": 88,
            "credentials": {"email": child.email},
            "extra": {"codex_7d_used_percent": 40, "codex_5h_used_percent": 100},
        }
        service = RotateService(sub2api=_FakeSub2Api([remote]))
        stats = await service.run_once(self.session, now=now, settings=self._settings(), accounts=[remote], in_test=True)
        self.assertEqual(stats["rotated"], 0)
        self.assertEqual(stats["scanned"], 0)

    async def test_identity_conflict_stops_rotate(self):
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        _owner, child, _workspace = await self._seed_child(email="conflict@icloud.com", remote_id="4002")
        binding = (
            await self.session.execute(
                select(ExternalBinding).where(ExternalBinding.remote_account_id == "4002")
            )
        ).scalar_one()
        binding.binding_state = "conflict"
        binding.last_error = "email mismatch"
        await self.session.commit()
        remote = {
            "id": 4002,
            "error_message": "account deactivated",
            "credentials": {"email": child.email},
        }
        child.last_reauth_code = "account_deactivated"
        await self.session.commit()
        service = RotateService(sub2api=_FakeSub2Api([remote]))
        service.kick_to_standby = AsyncMock()
        stats = await service.run_once(self.session, now=now, settings=self._settings(), accounts=[remote], in_test=True)
        self.assertEqual(stats["rotated"], 0)
        self.assertGreaterEqual(stats["conflict"], 1)
        service.kick_to_standby.assert_not_awaited()

    async def test_named_like_mother_still_rotates_when_local_child(self):
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        _owner, child, _workspace = await self._seed_child(email="named-mother@icloud.com", remote_id="4001", last_reauth="account_deactivated")
        remote = {
            "id": 4001,
            "name": "Team .2026.12 母号",
            "error_message": "account deactivated",
            "credentials": {"email": child.email},
        }
        vacancy = parse_policy_notice({"policy_notice": {"vacancy_ordinal": 1, "free_vacancy_threshold": 5}})
        service = RotateService(sub2api=_FakeSub2Api([remote]))
        service.kick_to_standby = AsyncMock(return_value={"success": True, "status": "standby", "vacancy": vacancy})
        refill = AsyncMock(return_value={"success": True, "child": {"email": "new@icloud.com"}})
        stats = await service.run_once(
            self.session,
            now=now,
            settings=self._settings(),
            accounts=[remote],
            refill=refill,
            in_test=True,
        )
        self.assertEqual(stats["rotated"], 1)
        self.assertEqual(stats["email"], child.email)

    async def test_running_workspace_operation_blocks_rotate(self):
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        _owner, child, workspace = await self._seed_child()
        await operation_store.create(
            self.session,
            op_type="rotate",
            workspace_id=workspace.id,
            email="other@icloud.com",
            input_payload={"workspace_id": workspace.id},
        )
        await self.session.commit()
        remote = {
            "id": 88,
            "credentials": {"email": child.email},
            "extra": {"codex_7d_used_percent": 100},
        }
        service = RotateService(sub2api=_FakeSub2Api([remote]))
        service.kick_to_standby = AsyncMock()
        stats = await service.run_once(self.session, now=now, settings=self._settings(), accounts=[remote], in_test=True)
        self.assertEqual(stats["rotated"], 0)
        service.kick_to_standby.assert_not_awaited()

    async def test_vacancy_unsafe_stops_refill(self):
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        _owner, child, workspace = await self._seed_child()
        vacancy = parse_policy_notice({"policy_notice": {"vacancy_ordinal": 5, "free_vacancy_threshold": 5}})
        service = RotateService(sub2api=_FakeSub2Api())
        service.kick_to_standby = AsyncMock(return_value={"success": True, "status": "standby", "vacancy": vacancy})
        refill = AsyncMock()
        op = await operation_store.create(
            self.session,
            op_type="rotate",
            workspace_id=workspace.id,
            account_id=child.id,
            email=child.email,
        )
        result = await service.run_rotate_saga(
            self.session,
            job_id=op.public_id,
            workspace_id=workspace.id,
            email=child.email,
            reason="weekly_limit",
            skip_confirm=True,
            usage={"seven_day": {"utilization": 100}},
            now=now,
            refill=refill,
            in_test=True,
        )
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "vacancy_not_safe_to_refill")
        self.assertTrue(result["needs_confirm"])
        refill.assert_not_awaited()

    async def test_force_refill_continues_when_vacancy_unsafe(self):
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        _owner, child, workspace = await self._seed_child()
        vacancy = parse_policy_notice({"policy_notice": {"vacancy_ordinal": 5, "free_vacancy_threshold": 5}})
        service = RotateService(sub2api=_FakeSub2Api())
        service.kick_to_standby = AsyncMock(return_value={"success": True, "status": "standby", "vacancy": vacancy})
        refill = AsyncMock(return_value={"success": True, "child": {"email": "new@icloud.com"}})
        op = await operation_store.create(
            self.session,
            op_type="rotate",
            workspace_id=workspace.id,
            account_id=child.id,
            email=child.email,
        )
        result = await service.run_rotate_saga(
            self.session,
            job_id=op.public_id,
            workspace_id=workspace.id,
            email=child.email,
            reason="weekly_limit",
            force_refill=True,
            skip_confirm=True,
            usage={"seven_day": {"utilization": 100}},
            now=now,
            refill=refill,
            in_test=True,
        )
        self.assertTrue(result["success"])
        refill.assert_awaited()

    async def test_rotate_saga_resume_after_kicked_does_not_kick_again(self):
        now = datetime(2026, 3, 29, 12, 0, tzinfo=timezone.utc)
        _owner, child, workspace = await self._seed_child(email="old@icloud.com")
        child.operational_state = "standby"
        child.local_purpose = "standby"
        await self.session.commit()
        op = await operation_store.create(
            self.session,
            op_type="rotate",
            workspace_id=workspace.id,
            email=child.email,
            input_payload={"workspace_id": workspace.id, "email": child.email, "reason": "weekly_limit"},
        )
        await operation_store.mark_step(self.session, op, "kicked", state="success", result={"success": True, "status": "standby"})
        await self.session.commit()
        service = RotateService(sub2api=_FakeSub2Api())
        service.kick_to_standby = AsyncMock(side_effect=AssertionError("saga must not kick again"))
        refill = AsyncMock(return_value={"success": True, "rotated": True, "child": {"email": "new@icloud.com"}})
        result = await service.run_rotate_saga(
            self.session,
            job_id=op.public_id,
            workspace_id=workspace.id,
            email=child.email,
            reason="weekly_limit",
            force_refill=True,
            skip_confirm=True,
            now=now,
            refill=refill,
            in_test=True,
        )
        self.assertTrue(result["success"])
        refill.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
