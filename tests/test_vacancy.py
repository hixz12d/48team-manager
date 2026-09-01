import unittest
from datetime import datetime
from unittest.mock import AsyncMock

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.vacancy import vacancy_service
from app.application.workspaces import WorkspaceService, is_access_token_error, workspace_error_is_fatal
from app.domain.vacancy import (
    chatgpt_member_ids,
    is_safe_to_refill,
    parse_policy_notice,
    pick_chatgpt_user_id,
    present_vacancy,
    summarize_for_message,
    to_local_naive,
)
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace
from app.persistence.models.vacancy import SeatVacancyEvent


class VacancyParseTests(unittest.TestCase):
    def test_missing_policy_notice_is_ignored(self):
        self.assertIsNone(parse_policy_notice({}))
        self.assertIsNone(parse_policy_notice({"ok": True}))
        self.assertIsNone(parse_policy_notice(None))

    def test_null_policy_notice_is_unknown_not_safe(self):
        parsed = parse_policy_notice({"policy_notice": None})
        presented = present_vacancy(parsed)
        self.assertTrue(parsed["policy_notice_null"])
        self.assertIsNone(parsed["is_free"])
        self.assertFalse(is_safe_to_refill(parsed))
        self.assertEqual(presented["status_label"], "无阈值")
        self.assertEqual(presented["comparison_text"], "policy_notice: null")
        self.assertEqual(presented["vacancy_ordinal_text"], "null")
        self.assertEqual(presented["tone"], "muted")
        self.assertEqual(presented["refill_label"], "勿自动补位")

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
        self.assertTrue(is_safe_to_refill(parsed))
        self.assertEqual(presented["comparison_text"], "3 < 5")
        self.assertEqual(presented["status_label"], "可释放")
        self.assertEqual(presented["refill_label"], "可自动补位")
        self.assertEqual(presented["billing_starts_at_text"], "2026-03-20 08:00:00")
        self.assertEqual(presented["expires_at_text"], "2026-03-27 08:00:00")

    def test_billing_notice_blocks_auto_refill(self):
        parsed = parse_policy_notice({
            "policy_notice": {
                "kind": "pending_replacement",
                "vacancy_ordinal": 1,
                "free_vacancy_threshold": 5,
                "replacement_required": True,
            },
            "billing_notice": {"private": "kept as evidence"},
        })
        presented = present_vacancy(parsed)
        self.assertTrue(parsed["is_free"])
        self.assertTrue(parsed["has_billing_notice"])
        self.assertTrue(parsed["replacement_required"])
        self.assertFalse(is_safe_to_refill(parsed))
        self.assertEqual(presented["status_label"], "有账单回执")
        self.assertEqual(presented["tone"], "warn")
        self.assertIn("private", parsed["billing_notice_json"] or "")

    def test_ordinal_at_or_above_threshold_is_locked(self):
        parsed = parse_policy_notice({
            "policy_notice": {
                "vacancy_ordinal": 5,
                "free_vacancy_threshold": 5,
            }
        })
        presented = present_vacancy(parsed)
        self.assertFalse(parsed["is_free"])
        self.assertFalse(is_safe_to_refill(parsed))
        self.assertEqual(presented["comparison_text"], "5 ≥ 5")
        self.assertEqual(presented["status_label"], "未释放")
        self.assertEqual(presented["tone"], "warn")
        self.assertIn("未释放", summarize_for_message(presented))

    def test_zero_ordinal_is_free(self):
        parsed = parse_policy_notice({
            "policy_notice": {
                "vacancy_ordinal": 0,
                "free_vacancy_threshold": 2,
            }
        })
        self.assertEqual(parsed["vacancy_ordinal"], 0)
        self.assertTrue(parsed["is_free"])
        self.assertTrue(is_safe_to_refill(parsed))

    def test_incomplete_policy_object_is_unknown(self):
        parsed = parse_policy_notice({"policy_notice": {"vacancy_ordinal": 1}})
        self.assertIsNotNone(parsed)
        self.assertIsNone(parsed["is_free"])
        self.assertFalse(is_safe_to_refill(parsed))

    def test_pick_user_id_prefers_user_prefix(self):
        self.assertEqual(
            pick_chatgpt_user_id({"id": "acct-1", "account_user_id": "user-abc"}),
            "user-abc",
        )
        self.assertEqual(pick_chatgpt_user_id({"account_user_id": "user-only"}), "user-only")
        self.assertIsNone(pick_chatgpt_user_id({"email": "a@b.com"}))
        self.assertEqual(
            chatgpt_member_ids({"id": "user-a", "account_user_id": "user-b"}, "user-a"),
            ["user-a", "user-b"],
        )

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

    async def _workspace(self) -> Workspace:
        owner = Account(
            email="owner@example.com",
            official_plan="unknown",
            local_purpose="mother",
            operational_state="active",
            access_token_encrypted="x",
        )
        self.session.add(owner)
        await self.session.flush()
        workspace = Workspace(
            official_workspace_id="11111111-1111-1111-1111-111111111111",
            owner_account_id=owner.id,
            status="active",
            seat_limit=5,
        )
        self.session.add(workspace)
        await self.session.flush()
        return workspace

    async def test_record_skips_duplicate_snapshot(self):
        workspace = await self._workspace()
        vacancy = parse_policy_notice({
            "policy_notice": {
                "vacancy_ordinal": 1,
                "free_vacancy_threshold": 4,
            }
        })
        first = await vacancy_service.record(
            self.session,
            workspace_id=workspace.id,
            user_id="user-1",
            email="kid@example.com",
            vacancy=vacancy,
        )
        second = await vacancy_service.record(
            self.session,
            workspace_id=workspace.id,
            user_id="user-1",
            email="kid@example.com",
            vacancy=vacancy,
        )
        count = (await self.session.execute(SeatVacancyEvent.__table__.select())).all()
        self.assertEqual(len(count), 1)
        self.assertEqual(first["id"], second["id"])

    async def test_delete_member_stores_and_returns_vacancy(self):
        workspace = await self._workspace()
        service = WorkspaceService()
        service.ensure_access_token = AsyncMock(return_value="tok")
        service.client = type("Svc", (), {})()
        service.client.delete_member = AsyncMock(return_value={
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
        result = await service.delete_member(self.session, workspace.id, "user-abc", email="kid@example.com")
        self.assertTrue(result["success"])
        self.assertIsNotNone(result.get("vacancy"))
        self.assertTrue(result["vacancy"]["is_free"])
        self.assertTrue(result["vacancy"]["safe_to_refill"])
        self.assertIn("2 < 5", result["message"])
        stored = (await self.session.execute(SeatVacancyEvent.__table__.select())).all()
        self.assertEqual(len(stored), 1)

    async def test_delete_member_retries_after_access_token_error(self):
        workspace = await self._workspace()
        service = WorkspaceService()
        service.ensure_access_token = AsyncMock(side_effect=["stale", "fresh"])
        service.client = type("Svc", (), {})()
        service.client.delete_member = AsyncMock(side_effect=[
            {"success": False, "status_code": 401, "error": "token is expired", "error_code": "token_expired"},
            {
                "success": True,
                "status_code": 200,
                "error": None,
                "data": {"policy_notice": {"vacancy_ordinal": 0, "free_vacancy_threshold": 2}},
                "vacancy": parse_policy_notice({"policy_notice": {"vacancy_ordinal": 0, "free_vacancy_threshold": 2}}),
            },
        ])
        result = await service.delete_member(self.session, workspace.id, "user-abc", email="kid@example.com")
        self.assertTrue(result["success"])
        self.assertEqual(service.client.delete_member.await_count, 2)
        self.assertEqual(service.ensure_access_token.await_count, 2)


class AccessTokenErrorTests(unittest.TestCase):
    def test_401_is_access_token_error(self):
        self.assertTrue(is_access_token_error({
            "status_code": 401,
            "error": "unauthorized",
        }))

    def test_token_invalidated_is_not_refreshable_access_token(self):
        self.assertFalse(workspace_error_is_fatal({
            "error_code": "token_expired",
            "error": "token is expired",
        }))
        self.assertTrue(workspace_error_is_fatal({
            "error_code": "token_invalidated",
            "error": "token has been invalidated",
        }))


if __name__ == "__main__":
    unittest.main()
