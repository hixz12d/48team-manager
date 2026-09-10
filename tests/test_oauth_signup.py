import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlencode, urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.onboard import OnboardService
from app.application.tokens import encrypt_secret
from app.application.workspaces import WorkspaceService
from app.integrations.openai.browser import reauth as browser
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace
from app.persistence.models.oauth import OAuthSession
from tests import test_onboard as fixtures


class InvitedOAuthSignupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        owner = Account(email="owner@example.com", local_purpose="mother", proxy=fixtures.PROXY,
                        access_token_encrypted=encrypt_secret("owner-token"))
        self.db.add(owner)
        await self.db.flush()
        self.workspace = Workspace(owner_account_id=owner.id, official_workspace_id=fixtures.WORKSPACE_UUID,
                                   status="active", seat_limit=5, name="Signup Team")
        self.db.add(self.workspace)
        await self.db.commit()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def run_signup(self, *, browser_ok=True, joined=True, bad_state=False, token_email="kid@icloud.com", invite_error=None, use_hme=False):
        client = fixtures._FakeChatGPT(auto_join=joined)
        client.invite_error = invite_error
        async def get_invites(*args, **kwargs):
            return {"success": True, "items": [{"email_address": i["email"], "role": "account-owner", "seat_type": "default"} for i in client.invites] if not client.members else [], "total": len(client.invites)}
        client.get_invites = get_invites
        registration = []
        def register(**kwargs):
            self.assertEqual(kwargs["start_url"], "https://chatgpt.com/accept-invite?token=test")
            self.assertFalse(kwargs["allow_sms"])
            self.assertEqual(len(client.invites), 1)
            registration.append(True)
            return {"ok": True, "access_token": "web-session", "session_token": "session"}
        service = OnboardService(workspaces=WorkspaceService(client=client), browser=register)

        async def callback(**kwargs):
            self.assertEqual(len(client.invites), 1)
            self.assertTrue(registration)
            self.assertFalse(kwargs["allow_signup"])
            self.assertFalse(kwargs["allow_sms"])
            self.assertNotIn("phone_source", kwargs)
            self.assertEqual(kwargs["phone"], "")
            self.assertEqual(kwargs["team_name"], "Signup Team")
            if not browser_ok:
                return {"ok": False, "error_code": "phone_verification_required"}
            state = parse_qs(urlparse(kwargs["authorize_url"]).query)["state"][0]
            return {"ok": True, "callback_url": "http://localhost:1455/auth/callback?" + urlencode({"state": "wrong" if bad_state else state, "code": "one-code"})}

        with (
            patch("app.integrations.mail.otp.wait_for_mailbox_item", return_value="https://chatgpt.com/accept-invite?token=test"),
            patch("app.application.invitation_flow.load_cf_config", new=AsyncMock(return_value={"base_url": "https://mail.test", "address": "mail@test.example", "admin_password": "secret"})),
            patch("app.application.oauth_signup.browser_slot.run_reauth_isolated", new=AsyncMock(side_effect=callback)) as run_browser,
            patch("app.application.oauth_signup.chatgpt_client.exchange_oauth_code", new=AsyncMock(return_value={"success": True, "access_token": "token", "refresh_token": "refresh"})) as exchange,
            patch("app.application.oauth_signup.jwt_parser.extract_email", return_value=token_email),
            patch.object(service, "_bind_phone", side_effect=AssertionError("SMS pool must not be bound")),
        ):
            result = await service.invite_and_onboard(
                self.db, workspace_id=self.workspace.id,
                email_line="" if use_hme else "kid@icloud.com----https://mail.example/pickup",
                phone_line="", oauth_signup=True, in_test=True,
            )
        return result, run_browser, exchange

    async def test_success_confirms_join_and_does_not_publish(self):
        result, browser_call, exchange = await self.run_signup()
        self.assertTrue(result["success"])
        self.assertFalse(result["pushed"])
        browser_call.assert_awaited_once()
        exchange.assert_awaited_once()
        stored = await self.db.scalar(select(OAuthSession))
        self.assertEqual(stored.purpose, "account_reauth")
        self.assertEqual(stored.status, "consumed")

    async def test_phone_requirement_stops_without_exchange(self):
        result, _, exchange = await self.run_signup(browser_ok=False)
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "phone_verification_required")
        exchange.assert_not_awaited()
        self.assertEqual((await self.db.scalar(select(OAuthSession))).status, "failed")

    async def test_bad_state_stops_before_exchange(self):
        result, _, exchange = await self.run_signup(bad_state=True)
        self.assertEqual(result["error_code"], "callback_invalid")
        exchange.assert_not_awaited()

    async def test_wrong_account_token_is_not_stored(self):
        result, _, _ = await self.run_signup(token_email="someone@example.com")
        self.assertEqual(result["error_code"], "token_identity_mismatch")
        child = await self.db.get(Account, result["child"]["id"])
        from app.application.tokens import decrypt_secret
        self.assertEqual(decrypt_secret(child.access_token_encrypted), "web-session")
        self.assertFalse(child.refresh_token_encrypted)

    async def test_callback_does_not_prove_membership(self):
        result, run_browser, exchange = await self.run_signup(joined=False)
        run_browser.assert_not_awaited()
        exchange.assert_not_awaited()
        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "not_joined")

    async def test_invite_failure_never_opens_oauth(self):
        result, run_browser, exchange = await self.run_signup(invite_error="forbidden")
        self.assertEqual(result["error_code"], "invite_failed")
        run_browser.assert_not_awaited()
        exchange.assert_not_awaited()

    async def test_empty_email_claims_hme_before_invite_and_labels_after_join(self):
        from app.application.resources import hme

        claimed = hme.ClaimedAlias("kid@icloud.com", "alias-id", "hme-account", 1)
        with (
            patch.object(hme, "maybe_claim_alias", new=AsyncMock(return_value=("kid@icloud.com----https://mail.example/pickup", claimed))) as claim,
            patch.object(hme, "mark_signup_started", new=AsyncMock()),
            patch.object(hme, "finalize_claim", new=AsyncMock()) as finalize,
        ):
            result, _, _ = await self.run_signup(use_hme=True)
        self.assertTrue(result["success"])
        self.assertEqual(claim.await_args.args[1], "")
        self.assertIs(finalize.await_args.args[1], claimed)
        self.assertTrue(finalize.await_args.args[2]["success"])
        self.assertEqual(finalize.await_args.args[3], "Signup Team")

    async def test_missing_token_identity_is_rejected(self):
        result, _, _ = await self.run_signup(token_email=None)
        self.assertEqual(result["error_code"], "token_identity_mismatch")


class BrowserNoSmsTests(unittest.TestCase):
    def test_phone_page_never_uses_provided_phone_or_pool(self):
        page = MagicMock()
        page.url = "https://auth.openai.com/add-phone"
        page.title.return_value = "Phone verification"
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context.return_value.pages = [page]
        with ExitStack() as stack:
            sync = stack.enter_context(patch("playwright.sync_api.sync_playwright"))
            sync.return_value.__enter__.return_value = playwright
            stack.enter_context(patch.object(browser, "Path"))
            proxy = stack.enter_context(patch.object(browser, "chrome_proxy_launch"))
            proxy.return_value.__enter__.return_value = {}
            stack.enter_context(patch.object(browser, "chromium_context_kwargs", return_value={"channel": "chrome"}))
            for name in ("goto_with_retries", "wait_cloudflare", "_snapshot_mailbox_codes", "_save_debug"):
                stack.enter_context(patch.object(browser, name))
            stack.enter_context(patch.object(browser, "_page_text", return_value="Phone verification"))
            for name in ("looks_like_about_you", "looks_like_deactivated", "looks_like_session_ended", "_pick_workspace"):
                stack.enter_context(patch.object(browser, name, return_value=False))
            take_phone = stack.enter_context(patch.object(browser, "take_pool_phone"))
            send_sms = stack.enter_context(patch.object(browser, "_submit_phone_sms"))
            wait_sms = stack.enter_context(patch.object(browser.sms_client, "wait_for_code"))
            result = browser.run_browser_oauth_reauth(
                email="test@example.com", password="test", proxy=fixtures.PROXY,
                authorize_url="https://auth.openai.com/oauth/authorize",
                allow_signup=True, allow_sms=False, phone="+15555555555",
                sms_url="https://sms.example", phone_source=MagicMock(),
                executable_path="C:/Chromix/chrome.exe",
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "phone_verification_required")
        take_phone.assert_not_called()
        send_sms.assert_not_called()
        wait_sms.assert_not_called()
        launch = playwright.chromium.launch_persistent_context.call_args.kwargs
        self.assertNotIn("channel", launch)
        self.assertEqual(launch["executable_path"], "C:/Chromix/chrome.exe")


if __name__ == "__main__":
    unittest.main()
