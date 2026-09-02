import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.console_actions import (
    reset_phone_cooldown,
    set_phone_status,
    update_account_proxy,
)
from app.application.identity import upsert_mother_account
from app.application.resources.phones import phone_pool_service
from app.application.resources.proxies import proxy_profile_service
from app.domain.resources import STATUS_ACTIVE, STATUS_DISABLED
from app.persistence.database import Base
from app.persistence.models.resources import PhonePool
from tests.helpers import make_client


class Batch3ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.session_maker = async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False)
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        self.session = self.session_maker()

    async def asyncTearDown(self):
        await self.session.close()
        await self.engine.dispose()

    async def test_mother_proxy_update_and_phone_controls(self):
        mother, _ = await upsert_mother_account(self.session, email="mother@example.com")
        await self.session.commit()

        bad = await update_account_proxy(self.session, mother.id, proxy="")
        self.assertFalse(bad["ok"])

        result = await update_account_proxy(self.session, mother.id, proxy="socks5://127.0.0.1:1080")
        self.assertTrue(result["ok"])
        self.assertEqual(result["proxy"], "set")
        self.assertIsNotNone(result["proxy_profile_id"])

        cleared = await update_account_proxy(self.session, mother.id, clear=True)
        self.assertTrue(cleared["ok"])
        self.assertEqual(cleared["proxy"], "none")

        phone = PhonePool(
            number="+15550001111",
            sms_url="https://sms.example/by_key?key=abc",
            status=STATUS_ACTIVE,
            used_count=1,
            risk_count=0,
            no_sms_streak=0,
        )
        self.session.add(phone)
        await self.session.commit()

        disabled = await set_phone_status(self.session, phone.id, STATUS_DISABLED)
        self.assertTrue(disabled["ok"])
        self.assertEqual(disabled["item"]["status"], STATUS_DISABLED)

        enabled = await set_phone_status(self.session, phone.id, STATUS_ACTIVE)
        self.assertTrue(enabled["ok"])
        self.assertEqual(enabled["item"]["status"], STATUS_ACTIVE)

        phone.last_used_at = phone.created_at
        await self.session.commit()
        reset = await reset_phone_cooldown(self.session, phone.id)
        self.assertTrue(reset["ok"])
        self.assertIsNone(reset["item"]["cooldown_until"])

    async def test_proxy_bindings_list(self):
        mother, _ = await upsert_mother_account(self.session, email="owner@example.com")
        profile = await proxy_profile_service.upsert_from_url(self.session, "http://127.0.0.1:8080", name="lab")
        mother.proxy = "http://127.0.0.1:8080"
        mother.proxy_profile_id = profile.id
        await self.session.commit()
        from app.application.console_actions import proxy_bindings

        payload = await proxy_bindings(self.session, profile.id)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["items"][0]["email"], "owner@example.com")


class Batch3ApiTests(unittest.TestCase):
    def test_endpoints_exist_and_guardrails(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})

            with patch(
                "app.application.console_actions.onboard_service.invite_and_onboard",
                new=AsyncMock(return_value={"success": True, "status": "success"}),
            ):
                response = client.post(
                    "/api/workspaces/999/onboard",
                    json={"email_line": "kid@example.com"},
                )
            self.assertEqual(response.status_code, 404)

            with patch(
                "app.application.console_actions.rotate_service.kick_and_refill",
                new=AsyncMock(return_value={"success": True, "status": "success"}),
            ):
                response = client.post(
                    "/api/workspaces/999/rotate",
                    json={"email": "kid@example.com"},
                )
            self.assertEqual(response.status_code, 404)

            response = client.post(
                "/api/workspaces/999/kick",
                json={"email": "kid@example.com"},
            )
            self.assertEqual(response.status_code, 404)

            response = client.post(
                "/api/workspaces/999/revoke-invite",
                json={"email": "kid@example.com"},
            )
            self.assertEqual(response.status_code, 404)

            created = client.post(
                "/api/resources/proxies",
                json={"url": "http://127.0.0.1:8080", "name": "lab"},
            ).json()["item"]
            bindings = client.get(f"/api/resources/proxies/{created['id']}/bindings")
            self.assertEqual(bindings.status_code, 200)
            self.assertEqual(bindings.json()["count"], 0)

            imported = client.post(
                "/api/resources/phones/import",
                json={"text": "+15551234567----https://sms.example/by_key?key=abc"},
            )
            self.assertEqual(imported.status_code, 200)
            phone_id = client.get("/api/resources/phones").json()["items"][0]["id"]
            patched = client.patch(f"/api/resources/phones/{phone_id}", json={"status": "disabled"})
            self.assertEqual(patched.status_code, 200)
            self.assertEqual(patched.json()["item"]["status"], "disabled")
            reset = client.post(f"/api/resources/phones/{phone_id}/reset-cooldown")
            self.assertEqual(reset.status_code, 200)

            overview = client.get("/api/overview")
            self.assertEqual(overview.status_code, 200)
            payload = overview.json()
            self.assertIn("attention", payload)

            js = client.get("/static/js/app.js").text
            self.assertIn("workspace.onboard", js)
            self.assertIn("account.kick", js)
            self.assertIn("phone.reset-cooldown", js)
            self.assertIn("openOnboard", js)
            self.assertIn("confirmDanger", js)

            accounts = client.get("/accounts").text
            self.assertIn("data-open-onboard", accounts)
            self.assertIn("onboard-sheet", accounts)
            self.assertIn("rotate-sheet", accounts)
