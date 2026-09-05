import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.mailbox import mailbox_readiness_snapshot, probe_account_mailbox
from app.application.resources.hme import HmeConfig
from app.integrations.mail.otp import list_mailbox_codes
from app.persistence.database import Base
from app.persistence.models.identity import Account


class MailboxReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.factory = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.db = self.factory()
        self.account = Account(email="child@icloud.com", local_purpose="child")
        self.db.add(self.account)
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def test_probe_verifies_exact_alias_and_accepts_empty_inbox(self):
        cfg = HmeConfig(base_url="http://icloud-hme:8081", service_token="service-token")
        with (
            patch("app.application.mailbox.load_config", new=AsyncMock(return_value=cfg)),
            patch("app.application.mailbox.hme_client.list_accounts", MagicMock(return_value=[{"id": "acc-1"}])),
            patch(
                "app.application.mailbox.hme_client.list_aliases",
                MagicMock(return_value=[{"email": "child@icloud.com", "active": True}]),
            ),
            patch(
                "app.application.mailbox.hme_client.list_inbox",
                MagicMock(return_value={"method": "cloudflare", "messages": []}),
            ),
        ):
            result = await probe_account_mailbox(self.db, self.account.id)
        self.assertTrue(result["ok"])
        self.assertEqual(result["message_count"], 0)
        self.assertFalse(result["end_to_end_verified"])
        await self.db.refresh(self.account)
        snapshot = mailbox_readiness_snapshot(self.account)
        self.assertTrue(snapshot["ready"])
        self.assertEqual(snapshot["method"], "cloudflare")

    async def test_probe_rejects_alias_from_different_account(self):
        cfg = HmeConfig(base_url="http://icloud-hme:8081", service_token="service-token", account_id="acc-1")
        with (
            patch("app.application.mailbox.load_config", new=AsyncMock(return_value=cfg)),
            patch("app.application.mailbox.hme_client.list_accounts", MagicMock(return_value=[{"id": "acc-1"}])),
            patch(
                "app.application.mailbox.hme_client.list_aliases",
                MagicMock(return_value=[{"email": "other@icloud.com", "active": True}]),
            ),
        ):
            result = await probe_account_mailbox(self.db, self.account.id)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "mailbox_unbound")


class HmeOtpTests(unittest.TestCase):
    def test_hme_messages_feed_existing_otp_parser(self):
        with patch(
            "app.application.mailbox.fetch_hme_mailbox",
            return_value={
                "method": "cloudflare",
                "messages": [{"id": "m-1", "subject": "Your verification code is 482913", "preview": ""}],
            },
        ):
            codes = list_mailbox_codes(
                email="child@icloud.com",
                hme_base_url="http://icloud-hme:8081",
                hme_service_token="service-token",
                hme_account_id="acc-1",
            )
        self.assertEqual(codes, ["482913"])
