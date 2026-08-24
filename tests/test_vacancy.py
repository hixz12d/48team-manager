import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import SeatVacancyEvent, Team
from app.services.team import TeamService
from app.services.vacancy import (
    parse_policy_notice,
    pick_chatgpt_user_id,
    present_vacancy,
    summarize_for_message,
    to_local_naive,
    vacancy_service,
)


class VacancyParseTests(unittest.TestCase):
    def test_missing_policy_notice_is_ignored(self):
        self.assertIsNone(parse_policy_notice({}))
        self.assertIsNone(parse_policy_notice({"ok": True}))
        self.assertIsNone(parse_policy_notice(None))

    def test_null_policy_notice_is_safe(self):
        parsed = parse_policy_notice({"policy_notice": None})
        presented = present_vacancy(parsed)
        self.assertTrue(parsed["policy_notice_null"])
        self.assertTrue(parsed["is_free"])
        self.assertEqual(presented["status_label"], "安全")
        self.assertEqual(presented["comparison_text"], "policy_notice: null")
        self.assertEqual(presented["vacancy_ordinal_text"], "null")
        self.assertEqual(presented["tone"], "ok")

    def test_ordinal_below_threshold_is_free(self):
        parsed = parse_policy_notice({
            "policy_notice": {
                "vacancy_ordinal": 3,
                "free_vacancy_threshold": 5,
                "billing_starts_at": "2026-03-20T00:00:00Z",
                "expires_at": "2026-03-27T00:00:00Z",
            }
        })
        presented = present_vacancy(parsed)
        self.assertTrue(parsed["is_free"])
        self.assertEqual(presented["comparison_text"], "3 < 5")
        self.assertEqual(presented["status_label"], "可释放")
        self.assertEqual(presented["billing_starts_at_text"], "2026-03-20 08:00:00")
        self.assertEqual(presented["expires_at_text"], "2026-03-27 08:00:00")

    def test_ordinal_at_or_above_threshold_is_locked(self):
        parsed = parse_policy_notice({
            "policy_notice": {
                "vacancy_ordinal": 5,
                "free_vacancy_threshold": 5,
            }
        })
        presented = present_vacancy(parsed)
        self.assertFalse(parsed["is_free"])
        self.assertEqual(presented["comparison_text"], "5 ≥ 5")
        self.assertEqual(presented["status_label"], "未释放")
        self.assertEqual(presented["tone"], "warn")
        self.assertIn("释放", summarize_for_message(presented) or "未释放")

    def test_zero_ordinal_is_free(self):
        parsed = parse_policy_notice({
            "policy_notice": {
                "vacancy_ordinal": 0,
                "free_vacancy_threshold": 2,
            }
        })
        self.assertEqual(parsed["vacancy_ordinal"], 0)
        self.assertTrue(parsed["is_free"])

    def test_incomplete_policy_object_is_ignored(self):
        self.assertIsNone(parse_policy_notice({"policy_notice": {"vacancy_ordinal": 1}}))

    def test_pick_user_id_prefers_user_prefix(self):
        self.assertEqual(
            pick_chatgpt_user_id({"id": "acct-1", "account_user_id": "user-abc"}),
            "user-abc",
        )
        self.assertEqual(pick_chatgpt_user_id({"account_user_id": "user-only"}), "user-only")
        self.assertIsNone(pick_chatgpt_user_id({"email": "a@b.com"}))

    def test_utc_z_converts_to_shanghai(self):
        self.assertEqual(
            to_local_naive("2026-03-20T00:00:00Z"),
            datetime(2026, 3, 20, 8, 0, 0),
        )


class VacancyPersistTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def _team(self) -> Team:
        team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            account_id="acc-1",
            max_members=5,
            current_members=2,
        )
        self.session.add(team)
        await self.session.flush()
        return team

    async def test_record_skips_duplicate_snapshot(self):
        team = await self._team()
        vacancy = parse_policy_notice({
            "policy_notice": {
                "vacancy_ordinal": 1,
                "free_vacancy_threshold": 4,
            }
        })
        first = await vacancy_service.record(
            self.session,
            team=team,
            user_id="user-1",
            email="kid@example.com",
            vacancy=vacancy,
        )
        second = await vacancy_service.record(
            self.session,
            team=team,
            user_id="user-1",
            email="kid@example.com",
            vacancy=vacancy,
        )
        count = (await self.session.execute(SeatVacancyEvent.__table__.select())).all()
        self.assertEqual(len(count), 1)
        self.assertEqual(first["id"], second["id"])

    async def test_delete_member_stores_and_returns_vacancy(self):
        team = await self._team()
        service = TeamService()
        service.ensure_access_token = AsyncMock(return_value="tok")
        service.chatgpt_service = type("Svc", (), {})()
        service.chatgpt_service.delete_member = AsyncMock(return_value={
            "success": True,
            "status_code": 200,
            "error": None,
            "data": {
                "policy_notice": {
                    "vacancy_ordinal": 2,
                    "free_vacancy_threshold": 5,
                    "billing_starts_at": "2026-03-20T00:00:00Z",
                    "expires_at": "2026-03-27T00:00:00Z",
                }
            },
        })
        service.sync_team_info = AsyncMock(return_value={"success": True})
        service.mark_team_email_mapping_removed = AsyncMock()
        service._reset_error_status = AsyncMock()

        result = await service.delete_team_member(
            team.id, "user-abc", self.session, email="kid@example.com"
        )
        self.assertTrue(result["success"])
        self.assertIsNotNone(result.get("vacancy"))
        self.assertTrue(result["vacancy"]["is_free"])
        self.assertIn("2 < 5", result["message"])
        stored = (await self.session.execute(SeatVacancyEvent.__table__.select())).all()
        self.assertEqual(len(stored), 1)

    async def test_attach_to_cards_uses_latest_event(self):
        team = await self._team()
        await vacancy_service.record(
            self.session,
            team=team,
            user_id="user-1",
            email="old@example.com",
            vacancy=parse_policy_notice({
                "policy_notice": {"vacancy_ordinal": 4, "free_vacancy_threshold": 4}
            }),
        )
        await vacancy_service.record(
            self.session,
            team=team,
            user_id="user-2",
            email="new@example.com",
            vacancy=parse_policy_notice({
                "policy_notice": {"vacancy_ordinal": 1, "free_vacancy_threshold": 4}
            }),
        )
        cards = await vacancy_service.attach_to_cards(self.session, [{"id": team.id}])
        self.assertEqual(len(cards[0]["vacancy_history"]), 2)
        self.assertEqual(cards[0]["vacancy"]["email"], "new@example.com")
        self.assertTrue(cards[0]["vacancy"]["is_free"])


if __name__ == "__main__":
    unittest.main()
