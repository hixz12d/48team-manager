import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

from sqlalchemy import select

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import ChildAccount, Sub2ApiUsageProbe
from app.services.auto_rotate import (
    AutoRotateService,
    DEFAULT_AUTO_REAUTH_ENABLED,
    DEFAULT_AUTO_ROTATE_DAILY_LIMIT,
    DEFAULT_AUTO_ROTATE_ENABLED,
    DEFAULT_AUTO_ROTATE_FORCE_REFILL,
    classify_rotate_reason,
    official_weekly_limit_full,
    official_weekly_reset_at,
    daily_auto_rotate_limit_reached,
    desired_schedulable,
    due_account_ids,
    initial_next_probe_at,
    probe_offset_seconds,
    should_clear_rate_limit,
)
from app.services.sub2api import Sub2ApiService


class UsageProbeDecisionTests(unittest.TestCase):
    def setUp(self):
        self.service = Sub2ApiService()

    def test_probe_offsets_cover_multiple_ids_without_selecting_all(self):
        now = datetime(2026, 3, 29, 12, 0, 0)
        stagger_seconds = 60 * 60
        account_ids = list(range(1, 51))
        offsets = {probe_offset_seconds(account_id, stagger_seconds) for account_id in account_ids}
        self.assertGreater(len(offsets), 10)

        next_map = {
            account_id: initial_next_probe_at(account_id, now, stagger_seconds)
            for account_id in account_ids
        }
        due = due_account_ids(account_ids, next_map, now, stagger_seconds, limit=3)
        self.assertLessEqual(len(due), 3)
        self.assertLess(len(due), len(account_ids))

        later = now + timedelta(minutes=5)
        due_later = due_account_ids(account_ids, next_map, later, stagger_seconds, limit=3)
        self.assertLessEqual(len(due_later), 3)
        self.assertLess(len(due_later), len(account_ids))

        window_end = now + timedelta(seconds=stagger_seconds)
        all_due = due_account_ids(account_ids, next_map, window_end, stagger_seconds, limit=3)
        self.assertEqual(len(all_due), 3)
        self.assertLess(len(all_due), len(account_ids))

    def test_weekly_reset_from_100_to_0_opens_schedulable(self):
        before = {
            "id": 12,
            "name": "Team .2026.12 母号",
            "status": "active",
            "schedulable": False,
            "credentials": {"email": "xiaozhudf.2026.12@gmail.com"},
            "extra": {"codex_7d_used_percent": 100, "codex_5h_used_percent": 12},
        }
        after = self.service.merge_usage_into_account(
            before,
            {
                "seven_day": {"utilization": 0, "resets_at": "2026-09-04T11:08:19+08:00"},
                "five_hour": {"utilization": 0, "resets_at": "2026-08-28T16:08:19+08:00"},
            },
        )
        snapshot = dict(after)
        snapshot.pop("schedulable", None)
        kind = self.service._schedule_state(snapshot)["kind"]
        self.assertEqual(kind, "ok")
        self.assertEqual(after["extra"]["codex_7d_used_percent"], 0)
        self.assertEqual(desired_schedulable(kind, after.get("schedulable")), True)

    def test_recovered_quota_clears_stale_sub_rate_limit_lock(self):
        future = (datetime.now(timezone.utc) + timedelta(days=4)).isoformat()
        recovered = {
            "id": 2840,
            "status": "active",
            "schedulable": True,
            "rate_limited_at": "2026-08-27T13:47:26+08:00",
            "rate_limit_reset_at": future,
            "extra": {"codex_7d_used_percent": 0, "codex_5h_used_percent": 0},
        }
        still_full = dict(recovered)
        still_full["extra"] = {"codex_7d_used_percent": 100, "codex_5h_used_percent": 0}
        token_dead = dict(recovered)
        token_dead["error_message"] = "Token revoked (401)"
        self.assertTrue(self.service.has_local_rate_limit_lock(recovered))
        self.assertTrue(should_clear_rate_limit("ok", recovered))
        self.assertFalse(should_clear_rate_limit("429", still_full))
        self.assertFalse(should_clear_rate_limit("401", token_dead))
        self.assertFalse(should_clear_rate_limit("ok", {"id": 1, "schedulable": True}))

    def test_five_hour_full_only_pauses_schedulable(self):
        soon = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        account = {
            "id": 22,
            "name": "Team .2026.12 子号 2",
            "status": "active",
            "schedulable": True,
            "credentials": {"email": "child22@example.com"},
            "extra": {
                "codex_7d_used_percent": 40,
                "codex_5h_used_percent": 100,
                "codex_5h_reset_at": soon,
            },
        }
        snapshot = dict(account)
        snapshot.pop("schedulable", None)
        kind = self.service._schedule_state(snapshot)["kind"]
        self.assertEqual(kind, "5h")
        self.assertEqual(desired_schedulable(kind, True), False)
        self.assertIsNone(desired_schedulable(kind, False))

    def test_401_does_not_open_schedulable(self):
        account = {
            "id": 2,
            "name": "Team .2026.15 子号 1",
            "status": "error",
            "schedulable": False,
            "error_message": "Token revoked (401): Encountered invalidated oauth token",
            "credentials": {"email": "eagle-snoop-4d@icloud.com"},
            "extra": {"codex_7d_used_percent": 0, "codex_5h_used_percent": 0},
        }
        snapshot = dict(account)
        snapshot.pop("schedulable", None)
        kind = self.service._schedule_state(snapshot)["kind"]
        self.assertEqual(kind, "401")
        self.assertIsNone(desired_schedulable(kind, False))
        self.assertIsNone(desired_schedulable(kind, True))

    def test_layer_two_and_three_defaults_are_off(self):
        self.assertFalse(DEFAULT_AUTO_REAUTH_ENABLED)
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


class UsageProbeRunTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        self.rotate = AutoRotateService()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_run_opens_schedulable_and_writes_child_probe(self):
        child = ChildAccount(email="kid@icloud.com", status="active", sub2api_account_id=12)
        self.session.add(child)
        await self.session.commit()
        account = {
            "id": 12,
            "name": "Team .2026.12 子号 1",
            "status": "active",
            "schedulable": False,
            "credentials": {"email": "kid@icloud.com"},
            "extra": {"codex_7d_used_percent": 100, "codex_5h_used_percent": 10},
        }
        now = datetime(2026, 3, 29, 12, 0, 0)
        self.session.add(
            Sub2ApiUsageProbe(
                sub2api_account_id=12,
                email="kid@icloud.com",
                next_probe_at=now,
                fail_count=0,
            )
        )
        await self.session.commit()

        with patch("app.services.auto_rotate.sub2api_service.list_status_accounts", AsyncMock(return_value=[account])), \
             patch("app.services.auto_rotate.sub2api_service.fetch_account_usage", AsyncMock(return_value={
                 "seven_day": {"utilization": 0},
                 "five_hour": {"utilization": 0},
             })), \
             patch("app.services.auto_rotate.sub2api_service.clear_account_rate_limit", AsyncMock()) as cleared, \
             patch("app.services.auto_rotate.sub2api_service.patch_account_fields", AsyncMock(return_value={
                 "account": {"schedulable": True}, "patched": True, "patch": {"schedulable": True}
             })) as patched, \
             patch("app.services.auto_rotate.invalidate_status_cache") as invalidate:
            stats = await self.rotate.run_usage_probe_once(
                self.session,
                now=now,
                settings={
                    "enabled": True,
                    "interval_minutes": 60,
                    "stagger_minutes": 60,
                    "batch_size": 1,
                    "force": True,
                    "scan_minutes": 2,
                },
            )

        self.assertEqual(stats["opened"], 1)
        self.assertEqual(stats["cleared"], 0)
        self.assertEqual(stats["account_ids"], [12])
        patched.assert_awaited_once()
        self.assertEqual(patched.await_args.args[2], {"schedulable": True})
        cleared.assert_not_awaited()
        invalidate.assert_called_once()
        await self.session.refresh(child)
        self.assertEqual(child.probe_status, "ok")
        row = await self.session.get(Sub2ApiUsageProbe, 1)
        self.assertEqual(row.fail_count, 0)
        self.assertGreater(row.next_probe_at, now)

    async def test_run_401_does_not_patch_schedulable(self):
        now = datetime(2026, 3, 29, 12, 0, 0)
        account = {
            "id": 9,
            "name": "Team .2026.15 子号 1",
            "status": "error",
            "schedulable": False,
            "error_message": "401 revoked",
            "credentials": {"email": "dead@icloud.com"},
            "extra": {"codex_7d_used_percent": 0},
        }
        self.session.add(
            Sub2ApiUsageProbe(sub2api_account_id=9, email="dead@icloud.com", next_probe_at=now, fail_count=0)
        )
        await self.session.commit()
        with patch("app.services.auto_rotate.sub2api_service.list_status_accounts", AsyncMock(return_value=[account])), \
             patch("app.services.auto_rotate.sub2api_service.fetch_account_usage", AsyncMock(return_value={
                 "seven_day": {"utilization": 0},
             })), \
             patch("app.services.auto_rotate.sub2api_service.clear_account_rate_limit", AsyncMock()) as cleared, \
             patch("app.services.auto_rotate.sub2api_service.patch_account_fields", AsyncMock()) as patched:
            stats = await self.rotate.run_usage_probe_once(
                self.session,
                now=now,
                settings={
                    "enabled": True,
                    "interval_minutes": 60,
                    "stagger_minutes": 60,
                    "batch_size": 1,
                    "force": True,
                    "scan_minutes": 2,
                },
            )
        self.assertEqual(stats["opened"], 0)
        self.assertEqual(stats["cleared"], 0)
        patched.assert_not_awaited()
        cleared.assert_not_awaited()

    async def test_run_clears_stale_rate_limit_then_keeps_schedulable(self):
        now = datetime(2026, 3, 29, 12, 0, 0)
        future = (now + timedelta(days=4)).isoformat()
        account = {
            "id": 2840,
            "name": "Team .2026.2 母号",
            "status": "active",
            "schedulable": True,
            "rate_limited_at": "2026-08-27T13:47:26+08:00",
            "rate_limit_reset_at": future,
            "credentials": {"email": "xiaozhudf.2026.2@gmail.com"},
            "extra": {"codex_7d_used_percent": 100, "codex_5h_used_percent": 40},
        }
        self.session.add(
            Sub2ApiUsageProbe(
                sub2api_account_id=2840,
                email="xiaozhudf.2026.2@gmail.com",
                next_probe_at=now,
                fail_count=0,
            )
        )
        await self.session.commit()
        with patch("app.services.auto_rotate.sub2api_service.list_status_accounts", AsyncMock(return_value=[account])), \
             patch("app.services.auto_rotate.sub2api_service.fetch_account_usage", AsyncMock(return_value={
                 "seven_day": {"utilization": 0},
                 "five_hour": {"utilization": 0},
             })), \
             patch("app.services.auto_rotate.sub2api_service.clear_account_rate_limit", AsyncMock(return_value={
                 "id": 2840,
                 "schedulable": True,
                 "rate_limited_at": None,
                 "rate_limit_reset_at": None,
             })) as cleared, \
             patch("app.services.auto_rotate.sub2api_service.patch_account_fields", AsyncMock()) as patched, \
             patch("app.services.auto_rotate.invalidate_status_cache") as invalidate:
            stats = await self.rotate.run_usage_probe_once(
                self.session,
                now=now,
                settings={
                    "enabled": True,
                    "interval_minutes": 60,
                    "stagger_minutes": 60,
                    "batch_size": 1,
                    "force": True,
                    "scan_minutes": 2,
                },
            )
        self.assertEqual(stats["opened"], 0)
        self.assertEqual(stats["cleared"], 1)
        cleared.assert_awaited_once()
        self.assertEqual(cleared.await_args.args[1], 2840)
        patched.assert_not_awaited()
        invalidate.assert_called_once()

    async def test_daily_limit_blocks_third_auto_rotate(self):
        from app.models import SeatEvent, Team

        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            account_id="acc-1",
            max_members=5,
            current_members=2,
            proxy="socks5h://127.0.0.1:1080",
            status="active",
        )
        self.session.add(team)
        await self.session.commit()
        now = datetime(2026, 3, 29, 12, 0, 0)
        for email in ("a@icloud.com", "b@icloud.com"):
            self.session.add(
                SeatEvent(
                    team_id=team.id,
                    email=email,
                    action="rotate",
                    success=True,
                    detail="auto",
                    created_at=now,
                )
            )
        await self.session.commit()
        self.assertEqual(await self.rotate.count_today_auto_rotates(self.session, team.id, now), 2)
        self.assertTrue(daily_auto_rotate_limit_reached(2, DEFAULT_AUTO_ROTATE_DAILY_LIMIT))

    async def _seed_weekly_limit_child(self, now):
        from app.models import Team

        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            account_id="acc-1",
            max_members=5,
            current_members=2,
            proxy="socks5h://127.0.0.1:1080",
            status="active",
        )
        self.session.add(team)
        await self.session.flush()
        child = ChildAccount(
            email="full@icloud.com",
            status="active",
            current_team_id=team.id,
            sub2api_account_id=88,
        )
        self.session.add(child)
        self.session.add(
            Sub2ApiUsageProbe(
                sub2api_account_id=88,
                email="full@icloud.com",
                next_probe_at=now,
                fail_count=0,
            )
        )
        await self.session.commit()
        account = {
            "id": 88,
            "name": "Team .2026.12 子号 1",
            "status": "active",
            "schedulable": False,
            "credentials": {"email": "full@icloud.com"},
            "extra": {"codex_7d_used_percent": 100, "codex_5h_used_percent": 0},
        }
        settings = {
            "auto_rotate_enabled": True,
            "auto_rotate_on_deactivated": True,
            "auto_rotate_on_weekly_limit": True,
            "auto_rotate_force_refill": False,
            "auto_rotate_daily_limit": 2,
        }
        return account, settings

    async def test_weekly_limit_skips_kick_when_official_usage_is_zero(self):
        now = datetime(2026, 3, 29, 12, 0, 0)
        account, settings = await self._seed_weekly_limit_child(now)
        with patch("app.services.auto_rotate.sub2api_service.list_status_accounts", AsyncMock(return_value=[account])), \
             patch("app.services.auto_rotate.sub2api_service.fetch_account_usage", AsyncMock(return_value={
                 "seven_day": {"utilization": 0, "resets_at": "2026-09-04T12:37:02+08:00"},
                 "five_hour": {"utilization": 0},
             })) as fetched, \
             patch("app.services.onboard_jobs.any_running", return_value=None), \
             patch("app.services.onboard_jobs.iter_running", return_value=[]), \
             patch("app.services.onboard.onboard_service.kick_and_refill", AsyncMock()) as kicked:
            stats = await self.rotate.run_auto_rotate_once(self.session, now=now, settings=settings)
        self.assertEqual(stats["skipped"], 1)
        self.assertEqual(stats["rotated"], 0)
        self.assertEqual(stats["email"], "full@icloud.com")
        fetched.assert_awaited_once()
        self.assertTrue(fetched.await_args.kwargs.get("force"))
        kicked.assert_not_awaited()
        row = (await self.session.execute(
            select(Sub2ApiUsageProbe).where(Sub2ApiUsageProbe.sub2api_account_id == 88)
        )).scalar_one()
        self.assertEqual(row.last_rotate_code, "weekly_limit_not_confirmed")

    async def test_weekly_limit_kicks_after_official_usage_still_full(self):
        now = datetime(2026, 3, 29, 12, 0, 0)
        account, settings = await self._seed_weekly_limit_child(now)
        with patch("app.services.auto_rotate.sub2api_service.list_status_accounts", AsyncMock(return_value=[account])), \
             patch("app.services.auto_rotate.sub2api_service.fetch_account_usage", AsyncMock(return_value={
                 "seven_day": {"utilization": 100},
                 "five_hour": {"utilization": 0},
             })) as fetched, \
             patch("app.services.onboard_jobs.any_running", return_value=None), \
             patch("app.services.onboard_jobs.iter_running", return_value=[]), \
             patch("app.services.onboard_jobs.create_job", return_value={"id": "job1"}), \
             patch("app.services.onboard_jobs.finish"), \
             patch("app.services.onboard.onboard_service.kick_and_refill", AsyncMock(return_value={
                 "success": True,
                 "rotated": True,
             })) as kicked:
            stats = await self.rotate.run_auto_rotate_once(self.session, now=now, settings=settings)
        self.assertEqual(stats["rotated"], 1)
        self.assertEqual(stats["skipped"], 0)
        fetched.assert_awaited_once()
        kicked.assert_awaited_once()
        self.assertEqual(kicked.await_args.kwargs["email"], "full@icloud.com")
        self.assertEqual(kicked.await_args.kwargs["reason"], "weekly_limit")
        self.assertIn("next_eligible_at", kicked.await_args.kwargs)

    def test_official_weekly_reset_at_parses_naive_local(self):
        when = official_weekly_reset_at({
            "seven_day": {"utilization": 100, "resets_at": "2026-09-04T12:37:02+08:00"},
        })
        self.assertEqual(when, datetime(2026, 9, 4, 12, 37, 2))

    async def _seed_reauth_team(self):
        from app.models import Team

        team = Team(
            email="xiaozhudf.2026.12@gmail.com",
            access_token_encrypted="x",
            account_id="acc-1",
            max_members=5,
            current_members=2,
            proxy="socks5h://127.0.0.1:1080",
            status="active",
            team_name="Team .2026.12",
        )
        self.session.add(team)
        await self.session.flush()
        return team

    async def test_auto_reauth_skips_standby_401(self):
        now = datetime(2026, 8, 28, 20, 0, 0)
        team = await self._seed_reauth_team()
        child = ChildAccount(
            email="basket.hurler_3a@icloud.com",
            status="standby",
            current_team_id=None,
            last_team_id=team.id,
            sub2api_account_id=2882,
            password_encrypted="pw",
            proxy="socks5h://127.0.0.1:1080",
        )
        self.session.add(child)
        await self.session.commit()
        account = {
            "id": 2882,
            "name": "Team .2026.2 子号 5",
            "status": "error",
            "schedulable": False,
            "error_message": "Token revoked (401)",
            "credentials": {"email": "basket.hurler_3a@icloud.com"},
        }
        with patch("app.services.auto_rotate.sub2api_service.list_status_accounts", AsyncMock(return_value=[account])), \
             patch("app.services.onboard_jobs.any_running", return_value=None), \
             patch("app.services.onboard_jobs.active_job_for_email", return_value=None), \
             patch("app.routes.seats.start_child_auto_reauth", AsyncMock()) as started:
            stats = await self.rotate.run_auto_reauth_once(
                self.session,
                now=now,
                settings={"auto_reauth_enabled": True, "auto_reauth_interval_minutes": 30},
            )
        self.assertEqual(stats["queued"], 0)
        self.assertEqual(stats["scanned"], 0)
        started.assert_not_awaited()

    async def test_auto_reauth_queues_blank_sub_email_via_account_id(self):
        now = datetime(2026, 8, 28, 20, 0, 0)
        team = await self._seed_reauth_team()
        child = ChildAccount(
            email="rollers-chocks3v@icloud.com",
            status="active",
            current_team_id=team.id,
            sub2api_account_id=2912,
            password_encrypted="pw",
            proxy="socks5h://127.0.0.1:1080",
        )
        self.session.add(child)
        await self.session.commit()
        account = {
            "id": 2912,
            "name": "Team .2026.12 子号 7",
            "status": "error",
            "schedulable": False,
            "error_message": "Token revoked (401)",
            "credentials": {},
        }
        with patch("app.services.auto_rotate.sub2api_service.list_status_accounts", AsyncMock(return_value=[account])), \
             patch("app.services.onboard_jobs.any_running", return_value=None), \
             patch("app.services.onboard_jobs.active_job_for_email", return_value=None), \
             patch("app.services.reauth.auto_reauth_plan", return_value={"auto": True, "reason": "ok"}), \
             patch("app.routes.seats.start_child_auto_reauth", AsyncMock(return_value={
                 "success": True,
                 "job_id": "job-blank",
             })) as started:
            stats = await self.rotate.run_auto_reauth_once(
                self.session,
                now=now,
                settings={"auto_reauth_enabled": True, "auto_reauth_interval_minutes": 30},
            )
        self.assertEqual(stats["queued"], 1)
        self.assertEqual(stats["email"], "rollers-chocks3v@icloud.com")
        started.assert_awaited_once()
        self.assertEqual(started.await_args.kwargs["email"], "rollers-chocks3v@icloud.com")


if __name__ == "__main__":
    unittest.main()
