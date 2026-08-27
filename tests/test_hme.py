import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.database import Base
from app.models import ChildAccount, HmeAliasLease, Team
from app.services.hme import (
    FREE_ACCOUNT_LABEL,
    HmeConfig,
    ClaimedAlias,
    is_serial_label,
    is_unoccupied_label,
    pick_next_unoccupied,
    resolve_team_tag,
    maybe_claim_alias,
    claim_next_alias,
    finalize_claim,
    active_leased_emails,
)
from app.services.onboard import OnboardService
from app.utils.time_utils import get_now


class SerialLabelTests(unittest.TestCase):
    def test_digits_and_alias_n_are_serial(self):
        self.assertTrue(is_serial_label("12"))
        self.assertTrue(is_serial_label(" 12 "))
        self.assertTrue(is_serial_label("别名 12"))
        self.assertTrue(is_serial_label("别名12"))
        self.assertTrue(is_serial_label("别名  3"))
        self.assertFalse(is_serial_label("GPT已使用"))
        self.assertFalse(is_serial_label("已使用"))
        self.assertFalse(is_serial_label("星尘"))
        self.assertFalse(is_serial_label(""))
        self.assertTrue(is_unoccupied_label(""))
        self.assertTrue(is_unoccupied_label("别名 7"))
        self.assertFalse(is_unoccupied_label("GPT已使用"))

    def test_pick_next_uses_created_at_not_serial(self):
        aliases = [
            {
                "email": "later@icloud.com",
                "anonymousId": "b",
                "label": "别名 3",
                "active": True,
                "createdAt": "2026-02-02T00:00:00Z",
            },
            {
                "email": "earlier@icloud.com",
                "anonymousId": "a",
                "label": "别名 12",
                "active": True,
                "createdAt": "2026-01-01T00:00:00Z",
            },
            {
                "email": "used@icloud.com",
                "anonymousId": "c",
                "label": "GPT已使用",
                "active": True,
                "createdAt": "2025-01-01T00:00:00Z",
            },
            {
                "email": "off@icloud.com",
                "anonymousId": "d",
                "label": "",
                "active": False,
                "createdAt": "2024-01-01T00:00:00Z",
            },
        ]
        picked = pick_next_unoccupied(aliases, [])
        self.assertEqual(picked["email"], "earlier@icloud.com")
        picked = pick_next_unoccupied(aliases, ["earlier@icloud.com"])
        self.assertEqual(picked["email"], "later@icloud.com")
        self.assertIsNone(pick_next_unoccupied(aliases, ["earlier@icloud.com", "later@icloud.com"]))

    def test_same_created_at_sorts_by_email(self):
        aliases = [
            {"email": "zeta@icloud.com", "anonymousId": "z", "label": "", "active": True, "createdAt": "2026-01-01T00:00:00Z"},
            {"email": "alpha@icloud.com", "anonymousId": "a", "label": "1", "active": True, "createdAt": "2026-01-01T00:00:00Z"},
        ]
        picked = pick_next_unoccupied(aliases, [])
        self.assertEqual(picked["email"], "alpha@icloud.com")

    def test_team_tag_prefers_name(self):
        team = Team(email="owner@example.com", access_token_encrypted="x", team_name=" 星尘 ")
        team.id = 3
        self.assertEqual(resolve_team_tag(team, {"3": "映射名"}), "星尘")
        team.team_name = ""
        self.assertEqual(resolve_team_tag(team, {"3": "映射名"}), "映射名")
        self.assertEqual(resolve_team_tag(team, {}), "owner")


class HmeClaimTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        self.cfg = HmeConfig(base_url="http://icloud-hme:8081", service_token="token-token-token", account_id="acc_1")
        self.aliases = [
            {"email": "one@icloud.com", "anonymousId": "id-one", "label": "别名 9", "active": True, "createdAt": "2026-01-01T00:00:00Z"},
            {"email": "two@icloud.com", "anonymousId": "id-two", "label": "", "active": True, "createdAt": "2026-01-02T00:00:00Z"},
        ]

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    def _patch_client(self):
        return patch.multiple(
            "app.services.hme",
            load_config=AsyncMock(return_value=self.cfg),
        )

    async def test_claim_skips_filled_email(self):
        email, claimed = await maybe_claim_alias(self.session, "manual@icloud.com")
        self.assertEqual(email, "manual@icloud.com")
        self.assertIsNone(claimed)

    async def test_claim_takes_oldest_unoccupied(self):
        with patch("app.services.hme.load_config", AsyncMock(return_value=self.cfg)), patch(
            "app.services.hme.hme_client.list_accounts", MagicMock(return_value=[{"id": "acc_1", "status": "active"}])
        ), patch(
            "app.services.hme.hme_client.list_aliases", MagicMock(return_value=self.aliases)
        ):
            claimed = await claim_next_alias(self.session, job_id="job1", purpose="onboard")
        self.assertEqual(claimed.email, "one@icloud.com")
        leased = await active_leased_emails(self.session)
        self.assertIn("one@icloud.com", leased)

    async def test_second_claim_does_not_reuse_lease(self):
        with patch("app.services.hme.load_config", AsyncMock(return_value=self.cfg)), patch(
            "app.services.hme.hme_client.list_accounts", MagicMock(return_value=[{"id": "acc_1", "status": "active"}])
        ), patch(
            "app.services.hme.hme_client.list_aliases", MagicMock(return_value=self.aliases)
        ):
            first = await claim_next_alias(self.session, job_id="job1")
            second = await claim_next_alias(self.session, job_id="job2")
        self.assertEqual(first.email, "one@icloud.com")
        self.assertEqual(second.email, "two@icloud.com")
        self.assertNotEqual(first.lease_id, second.lease_id)

    async def test_claim_skips_existing_child_email(self):
        self.session.add(ChildAccount(email="one@icloud.com"))
        await self.session.commit()
        with patch("app.services.hme.load_config", AsyncMock(return_value=self.cfg)), patch(
            "app.services.hme.hme_client.list_accounts", MagicMock(return_value=[{"id": "acc_1", "status": "active"}])
        ), patch(
            "app.services.hme.hme_client.list_aliases", MagicMock(return_value=self.aliases)
        ):
            claimed = await claim_next_alias(self.session, job_id="job-child")
        self.assertEqual(claimed.email, "two@icloud.com")

    async def test_finalize_success_tags_then_releases(self):
        claimed = ClaimedAlias(email="one@icloud.com", anonymous_id="id-one", account_id="acc_1", lease_id=0)
        lease = HmeAliasLease(
            email=claimed.email,
            anonymous_id=claimed.anonymous_id,
            account_id=claimed.account_id,
            expires_at=get_now() + timedelta(minutes=25),
            created_at=get_now(),
        )
        self.session.add(lease)
        await self.session.commit()
        claimed.lease_id = lease.id
        with patch("app.services.hme.load_config", AsyncMock(return_value=self.cfg)), patch(
            "app.services.hme.hme_client.set_local_label", MagicMock()
        ) as tagged:
            await finalize_claim(self.session, claimed, {"success": True}, "星尘")
            tagged.assert_called_once()
            self.assertEqual(tagged.call_args.args[3], "星尘")
        leased = await active_leased_emails(self.session)
        self.assertNotIn("one@icloud.com", leased)

    async def test_finalize_failure_releases_without_tag(self):
        claimed = ClaimedAlias(email="one@icloud.com", anonymous_id="id-one", account_id="acc_1", lease_id=0)
        lease = HmeAliasLease(
            email=claimed.email,
            anonymous_id=claimed.anonymous_id,
            account_id=claimed.account_id,
            expires_at=get_now() + timedelta(minutes=25),
            created_at=get_now(),
        )
        self.session.add(lease)
        await self.session.commit()
        claimed.lease_id = lease.id
        with patch("app.services.hme.hme_client.set_local_label", MagicMock()) as tagged:
            await finalize_claim(self.session, claimed, {"success": False}, "星尘")
            tagged.assert_not_called()
        leased = await active_leased_emails(self.session)
        self.assertNotIn("one@icloud.com", leased)

    async def test_finalize_failure_tags_when_child_exists(self):
        claimed = ClaimedAlias(email="one@icloud.com", anonymous_id="id-one", account_id="acc_1", lease_id=0)
        lease = HmeAliasLease(
            email=claimed.email,
            anonymous_id=claimed.anonymous_id,
            account_id=claimed.account_id,
            expires_at=get_now() + timedelta(minutes=25),
            created_at=get_now(),
        )
        self.session.add(lease)
        self.session.add(ChildAccount(email="one@icloud.com"))
        await self.session.commit()
        claimed.lease_id = lease.id
        with patch("app.services.hme.load_config", AsyncMock(return_value=self.cfg)), patch(
            "app.services.hme.hme_client.set_local_label", MagicMock()
        ) as tagged:
            await finalize_claim(self.session, claimed, {"success": False}, "")
            tagged.assert_called_once()
            self.assertEqual(tagged.call_args.args[3], FREE_ACCOUNT_LABEL)
        leased = await active_leased_emails(self.session)
        self.assertNotIn("one@icloud.com", leased)


class HmeOnboardWrapperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()
        self.team = Team(
            email="owner@example.com",
            access_token_encrypted="x",
            team_name="星尘",
            proxy="socks5h://127.0.0.1:1080",
            status="active",
            account_role="account-owner",
        )
        self.session.add(self.team)
        await self.session.commit()
        await self.session.refresh(self.team)

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_empty_email_claims_and_tags_team_name(self):
        claimed = ClaimedAlias(email="auto@icloud.com", anonymous_id="id-a", account_id="acc_1", lease_id=1)
        service = OnboardService()
        service._invite_and_onboard_impl = AsyncMock(return_value={"success": True, "message": "ok"})
        service._load_team = AsyncMock(return_value=self.team)
        with patch("app.services.hme.maybe_claim_alias", AsyncMock(return_value=("auto@icloud.com", claimed))), patch(
            "app.services.hme.finalize_claim", AsyncMock()
        ) as fin, patch(
            "app.services.hme.load_config", AsyncMock(return_value=HmeConfig(team_tag_map={}))
        ):
            result = await service.invite_and_onboard(self.session, team_id=self.team.id, email_line="")
        self.assertTrue(result["success"])
        self.assertEqual(fin.await_args.args[3], "星尘")
        self.assertEqual(service._invite_and_onboard_impl.await_args.kwargs["email_line"], "auto@icloud.com")

    async def test_manual_email_does_not_claim(self):
        service = OnboardService()
        service._invite_and_onboard_impl = AsyncMock(return_value={"success": True})
        with patch("app.services.hme.maybe_claim_alias", AsyncMock(return_value=("manual@icloud.com", None))) as claim, patch(
            "app.services.hme.finalize_claim", AsyncMock()
        ) as fin:
            await service.invite_and_onboard(self.session, team_id=self.team.id, email_line="manual@icloud.com")
        claim.assert_awaited()
        self.assertIsNone(fin.await_args.args[1])

    async def test_failure_does_not_tag(self):
        claimed = ClaimedAlias(email="auto@icloud.com", anonymous_id="id-a", account_id="acc_1", lease_id=1)
        service = OnboardService()
        service._invite_and_onboard_impl = AsyncMock(return_value={"success": False, "error": "boom"})
        with patch("app.services.hme.maybe_claim_alias", AsyncMock(return_value=("auto@icloud.com", claimed))), patch(
            "app.services.hme.finalize_claim", AsyncMock()
        ) as fin:
            result = await service.invite_and_onboard(self.session, team_id=self.team.id, email_line="")
        self.assertFalse(result["success"])
        self.assertEqual(fin.await_args.args[3], "")

    async def test_free_success_tags_gpt(self):
        claimed = ClaimedAlias(email="free@icloud.com", anonymous_id="id-f", account_id="acc_1", lease_id=2)
        service = OnboardService()
        service._register_free_account_impl = AsyncMock(return_value={"success": True})
        with patch("app.services.hme.maybe_claim_alias", AsyncMock(return_value=("free@icloud.com", claimed))), patch(
            "app.services.hme.finalize_claim", AsyncMock()
        ) as fin:
            await service.register_free_account(self.session, email_line="")
        self.assertEqual(fin.await_args.args[3], FREE_ACCOUNT_LABEL)
