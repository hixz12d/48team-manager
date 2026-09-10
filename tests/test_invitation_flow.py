import unittest
from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.onboard import OnboardService
from app.application.invitation_flow import prepare
from app.application.tokens import encrypt_secret
from app.integrations.mail.otp import extract_invite_url
from app.integrations.sms.client import parse_optional_sms
from app.integrations.openai.browser import reauth as browser
from app.persistence.database import Base
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership

CF = {"base_url": "https://mail.example", "address": "mail@example.com", "admin_password": "test"}


class InviteLinkTests(unittest.TestCase):
    def test_html_entities_preserve_parameters(self):
        self.assertEqual(extract_invite_url('<a href="https://chatgpt.com/accept-invite?token=t&amp;id=w">Join</a>'), "https://chatgpt.com/accept-invite?token=t&id=w")

    def test_only_invitation_routes_are_accepted(self):
        for url in ("https://chatgpt.com/privacy", "https://chatgpt.com/", "http://chatgpt.com/invite/x", "https://chatgpt.com.evil/invite/x", "https://evil.example/invite/x", "https://chatgpt.com/auth/login?next=https%3A%2F%2Fevil.example%2Finvite", "https://chatgpt.com/auth/login?next=%2Fhome"):
            with self.subTest(url=url):
                self.assertIsNone(extract_invite_url(url))

    def test_login_wrapped_invitation(self):
        url = "https://chatgpt.com/auth/login?next=%2Faccept-invite%3Ftoken%3Dx"
        self.assertEqual(extract_invite_url(url), url)

    def test_optional_sms_input(self):
        self.assertEqual(parse_optional_sms(""), ("", ""))
        self.assertEqual(parse_optional_sms("+15555555555----https://sms.example/?key=t")[0], "+15555555555")
        for value in ("+15555555555", "+15555555555----http://sms.example/", "+15555555555----https://user:secret@sms.example/", "bad----https://sms.example/"):
            with self.assertRaises(ValueError) as caught:
                parse_optional_sms(value)
            self.assertNotIn(value, str(caught.exception))


class ResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with self.engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        owner = Account(email="owner@example.com", local_purpose="mother", proxy="socks5h://127.0.0.1:1080")
        self.db.add(owner)
        await self.db.flush()
        self.workspace = Workspace(owner_account_id=owner.id, status="active", seat_limit=2, name="Test")
        self.db.add(self.workspace)
        await self.db.commit()
        self.service = OnboardService()

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def seed_child(self, email, state="invited"):
        child = Account(email=email, local_purpose="child", operational_state="available", auth_state="oauth_required")
        self.db.add(child)
        await self.db.flush()
        self.db.add(WorkspaceMembership(workspace_id=self.workspace.id, account_id=child.id, membership_state=state, local_purpose="child", official_role="owner"))
        await self.db.commit()
        return child

    async def test_pending_alias_is_reused_even_without_hme_configuration(self):
        child = await self.seed_child("same@example.com")
        with (
            patch("app.application.invitation_flow.load_cf_config", new=AsyncMock(return_value=CF)),
            patch.object(self.service.workspaces, "lookup_live_member", new=AsyncMock(return_value=({"success": True, "members": [{}, {}]}, {"status": "invited"}))),
            patch.object(self.service, "pick_replacement", new=AsyncMock()) as pick,
        ):
            email, blocked = await prepare(self.service, self.db, workspace_id=self.workspace.id, email_line="", phone_line="", role="owner", seat_intent="premium", skip_invite=False)
        self.assertIsNone(blocked)
        self.assertEqual(email, child.email)
        pick.assert_not_awaited()

    async def test_multiple_partial_children_require_selection(self):
        await self.seed_child("one@example.com")
        await self.seed_child("two@example.com", "joined")
        email, blocked = await prepare(self.service, self.db, workspace_id=self.workspace.id, email_line="", phone_line="", role="owner", seat_intent="premium", skip_invite=False)
        self.assertEqual(blocked["error_code"], "resume_account_required")

    async def test_missing_link_stops_without_registration_or_oauth(self):
        child = await self.seed_child("same@example.com")
        registration = MagicMock(side_effect=AssertionError("No registration without invitation link"))
        self.service.browser = registration
        with (
            patch("app.application.invitation_flow.load_cf_config", new=AsyncMock(return_value=CF)),
            patch("app.application.onboard.load_cf_config", new=AsyncMock(return_value=CF)),
            patch.object(self.service.workspaces, "lookup_live_member", new=AsyncMock(return_value=({"success": True}, {"status": "invited", "role": "account-owner", "seat_type": "prolite"}))),
            patch("app.integrations.mail.otp.wait_for_mailbox_item", return_value=None),
            patch("app.application.oauth_signup.browser_slot.run_reauth_isolated", new=AsyncMock()) as oauth,
        ):
            result = await self.service.invite_and_onboard(self.db, workspace_id=self.workspace.id, email_line="", oauth_signup=True, seat_intent="premium", in_test=True)
        self.assertEqual(result["error_code"], "invite_link_missing")
        self.assertEqual(result["child"]["id"], child.id)
        registration.assert_not_called()
        oauth.assert_not_awaited()

    async def test_joined_child_continues_oauth_without_inviting_or_registering(self):
        child = await self.seed_child("joined@example.com", "joined")
        child.password_encrypted = encrypt_secret("password")
        registration = MagicMock(side_effect=AssertionError("Already joined"))
        self.service.browser = registration
        member = {"email": child.email, "role": "owner", "seat_type": "prolite"}
        with (
            patch("app.application.invitation_flow.load_cf_config", new=AsyncMock(return_value=CF)),
            patch.object(self.service.workspaces, "lookup_live_member", new=AsyncMock(return_value=({"success": True}, {**member, "status": "joined"}))),
            patch.object(self.service, "_confirm_joined", new=AsyncMock(return_value=member)),
            patch.object(self.service.workspaces, "invite_member", new=AsyncMock()) as invite,
            patch("app.application.resources.hme.claim_next_alias", new=AsyncMock()) as claim,
            patch("app.application.oauth_signup.run_invited_oauth_signup", new=AsyncMock(return_value={"ok": False, "error_code": "phone_verification_required"})) as oauth,
        ):
            result = await self.service.invite_and_onboard(self.db, workspace_id=self.workspace.id, email_line="", oauth_signup=True, seat_intent="premium", in_test=True)
        self.assertFalse(result["success"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["child"]["id"], child.id)
        oauth.assert_awaited_once()
        registration.assert_not_called()
        invite.assert_not_awaited()
        claim.assert_not_awaited()

    async def test_mother_is_protected_before_claim(self):
        result = await self.service.invite_and_onboard(self.db, workspace_id=self.workspace.id, email_line="owner@example.com", oauth_signup=True)
        self.assertEqual(result["error_code"], "primary_mother_protected")


class ConditionalSmsTests(unittest.TestCase):
    def run_browser(self, urls, *, phone_visible=False, otp_visible=False):
        page = MagicMock()
        page.url = urls[0]
        page.title.return_value = "OpenAI"
        playwright = MagicMock()
        playwright.chromium.launch_persistent_context.return_value.pages = [page]
        with ExitStack() as stack:
            sync = stack.enter_context(patch("playwright.sync_api.sync_playwright"))
            sync.return_value.__enter__.return_value = playwright
            stack.enter_context(patch.object(browser, "Path"))
            proxy = stack.enter_context(patch.object(browser, "chrome_proxy_launch"))
            proxy.return_value.__enter__.return_value = {}
            stack.enter_context(patch.object(browser, "chromium_context_kwargs", return_value={}))
            for name in ("goto_with_retries", "wait_cloudflare", "_snapshot_mailbox_codes", "_save_debug", "_fill_phone_number"):
                stack.enter_context(patch.object(browser, name))
            stack.enter_context(patch.object(browser, "_page_text", return_value=""))
            stack.enter_context(patch.object(browser, "phone_page_outcome", return_value=("", "")))
            stack.enter_context(patch.object(browser, "_visible", return_value=phone_visible))
            otp = MagicMock()
            stack.enter_context(patch.object(browser, "_find_otp", return_value=otp if otp_visible else None))
            for name in ("looks_like_about_you", "looks_like_deactivated", "looks_like_session_ended", "_pick_workspace"):
                stack.enter_context(patch.object(browser, name, return_value=False))
            send = stack.enter_context(patch.object(browser, "_submit_phone_sms"))
            poll = stack.enter_context(patch.object(browser.sms_client, "wait_for_code", return_value="123456"))
            def click(*args, **kwargs):
                if len(urls) > 1:
                    page.url = urls[-1]
                return True
            stack.enter_context(patch.object(browser, "_click_first", side_effect=click))
            result = browser.run_browser_oauth_reauth(
                email="test@example.com", password="password", proxy="socks5h://127.0.0.1:1080",
                authorize_url="https://auth.openai.com/oauth/authorize", allow_signup=False,
                allow_sms=True, phone="+15555555555", sms_url="https://sms.example/receipt?key=test",
                max_sms_submissions=1, max_sms_code_submissions=1,
            )
        return result, send, poll

    def test_no_phone_page_does_not_contact_sms_provider(self):
        result, send, poll = self.run_browser(["http://localhost:1455/auth/callback?code=test&state=s"])
        self.assertTrue(result["ok"])
        send.assert_not_called()
        poll.assert_not_called()

    def test_phone_page_does_not_resend_indefinitely(self):
        result, send, poll = self.run_browser(["https://auth.openai.com/add-phone"], phone_visible=True)
        self.assertEqual(result["error_code"], "sms_send_limit")
        send.assert_called_once()
        poll.assert_not_called()

    def test_sms_code_is_read_only_on_phone_verification_page(self):
        result, send, poll = self.run_browser(["https://auth.openai.com/phone-verification", "http://localhost:1455/auth/callback?code=test&state=s"], otp_visible=True)
        self.assertTrue(result["ok"])
        self.assertTrue(result["sms_verified"])
        send.assert_not_called()
        poll.assert_called_once()

    def test_rejected_sms_code_is_not_submitted_again(self):
        result, _, poll = self.run_browser(["https://auth.openai.com/phone-verification"], otp_visible=True)
        self.assertEqual(result["error_code"], "sms_code_rejected")
        poll.assert_called_once()


if __name__ == "__main__":
    unittest.main()
