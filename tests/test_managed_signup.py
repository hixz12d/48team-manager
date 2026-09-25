"""Managed signup budgets, identity guards and legacy/new-runner handoff."""
from contextlib import ExitStack
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.openai.browser import onboard
from app.integrations.openai.browser.signup import SignupBridge, signup_assets, signup_profile
from app.integrations.openai.browser.signup_state import SignupState, trusted_url
from app.integrations.openai.browser.signup_readiness import SignupReadiness


class SignupStateTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.codes = MagicMock(return_value=["123456", "005239"])
        self.reports = []
        self.state = SignupState(email="test@icloud.com", password="private-password",
            profile={"name": "Test User", "birthday": "1996-01-02"}, version="fixture",
            read_codes=self.codes, baseline=["123456"], report=lambda *args: self.reports.append(args),
            clock=lambda: self.now)
        self.url = "https://auth.openai.com/email-verification"

    def send(self, kind, **kwargs):
        return self.state.handle({"type": kind, "version": "fixture", "jobId": self.state.id, **kwargs}, self.url)

    def reserve(self, stage="otp"):
        return self.send("claim", stage=stage, reserve=True)

    def test_state_is_bound_to_version_origin_and_current_job(self):
        for url in ["http://auth.openai.com/x", "https://auth.openai.com:443/x", "https://evil.invalid", "https://auth.openai.com@evil.invalid"]:
            self.assertFalse(trusted_url(url))
            self.assertNotIn("password", self.state.handle({"type": "state", "version": "fixture"}, url))
        self.assertNotIn("password", self.send("state", version="old"))
        self.assertNotIn("password", self.send("claim", jobId="stale", stage="otp", reserve=True))
        self.assertEqual(self.send("state")["password"], "private-password")
        self.send("pause", reason="private-password")
        self.assertNotIn("password", self.send("state"))
        self.assertNotIn("private-password", json.dumps(self.reports))

    def test_otp_baseline_and_unsent_reservation_preserve_code(self):
        self.assertIsNone(self.send("code")["code"])
        self.send("event", event="page", stage="otp")
        self.assertEqual(self.send("code")["code"], "005239")
        first = self.reserve()
        self.assertFalse(self.reserve()["granted"])
        self.assertFalse(self.send("finish-click", stage="otp", token="wrong", sent=False)["accepted"])
        self.assertFalse(self.send("finish-click", stage="otp", token=first["token"], sent="false")["accepted"])
        self.send("pause")
        self.assertTrue(self.send("finish-click", stage="otp", token=first["token"], sent=False)["accepted"])
        self.assertEqual(self.state.pending_code, "005239")
        self.assertEqual(self.state.attempts["otp"], 0)
        self.assertEqual(self.state.claims, {})
        self.assertFalse(self.send("state")["active"])
        self.codes.assert_called_once()

    def test_two_second_retries_keep_one_code_and_cannot_reset_the_budget(self):
        self.send("event", event="page", stage="otp")
        self.send("code")
        token = self.reserve()["token"]
        self.send("finish-click", stage="otp", token=token, sent=True)
        self.assertEqual(self.state.used_codes, {"123456", "005239"})
        for _ in range(2):
            self.now += 1
            self.assertTrue(self.send("retry-continue", stage="otp", token=token)["wait"])
            self.now += 1
            next_click = self.send("retry-continue", stage="otp", token=token)
            self.assertTrue(next_click["granted"])
            self.assertFalse(self.send("retry-continue", stage="otp", token=token)["granted"])
            token = next_click["token"]
            self.send("finish-click", stage="otp", token=token, sent=True)
        self.now += 3
        self.assertFalse(self.send("retry-continue", stage="otp", token=token)["granted"])
        self.send("event", event="page", stage="otp")  # A page reload does not reset budgets.
        self.assertFalse(self.reserve()["granted"])
        self.assertIsNone(self.send("code")["code"])
        self.assertEqual(self.state.attempts["otp"], 3)
        self.assertEqual(self.state.retries["otp"], 2)
        self.codes.assert_called_once()

    def test_retry_cancel_restores_the_original_submission_guard(self):
        token = self.reserve("profile")["token"]
        self.send("finish-click", stage="profile", token=token, sent=True)
        claims = dict(self.state.claims)
        self.now += 3
        retry = self.send("retry-continue", stage="profile", token=token)
        self.send("finish-click", stage="profile", token=retry["token"], sent=False)
        self.assertEqual(self.state.claims, claims)
        self.assertEqual(self.state.attempts["profile"], 1)
        self.assertEqual(self.state.retries["profile"], 0)
        self.assertFalse(self.reserve("profile")["granted"])

    def test_loading_validation_page_change_and_phone_stop_retries(self):
        for event, outcome in [("click_response", "submitted"), ("click_response", "loading"), ("click_response", "validation"), ("page", None), ("phone", None), ("captcha", None), ("rate_limit", None)]:
            with self.subTest(event=event, outcome=outcome):
                self.setUp()
                token = self.reserve()["token"]
                self.send("finish-click", stage="otp", token=token, sent=True)
                self.send("event", event=event, outcome=outcome, stage="profile" if event == "page" else "otp", text="secret error")
                self.now += 3
                self.assertFalse(self.send("retry-continue", stage="otp", token=token).get("granted", False))
                self.assertNotIn("secret error", json.dumps(self.state.events))

    def test_invitation_never_uses_generic_entry_fallback(self):
        self.url = "https://chatgpt.com/"
        self.now += 20
        self.state.invite_entry = True
        self.assertFalse(self.send("entry-fallback")["granted"])
        self.state.invite_entry = False
        self.assertTrue(self.send("entry-fallback")["granted"])
        self.assertFalse(self.send("entry-fallback")["granted"])

    def test_completion_requires_host_session_verification_and_correct_email(self):
        self.url = "https://chatgpt.com/done"
        self.send("complete", email="test@icloud.com")
        self.assertEqual(self.state.status, "running")
        self.assertEqual(self.state.boundary_url, self.url)
        self.send("complete", email="wrong@icloud.com")
        self.assertEqual(self.state.error_code, "token_identity_mismatch")

    def test_lost_acknowledgement_never_allows_a_new_click(self):
        token = self.reserve()["token"]
        self.now += 3
        self.assertFalse(self.send("retry-continue", stage="otp", token=token)["granted"])
        self.now += 46
        self.assertFalse(self.send("claim", stage="otp", prepare=True)["granted"])
        self.assertEqual(self.state.error_code, "registration_submit_timeout")


class SignupReadinessTests(unittest.TestCase):
    def setUp(self):
        self.now = 10.0
        self.gate = SignupReadiness(clock=lambda: self.now)
        self.snapshot = {"ready": True, "document": 1, "url": "https://chatgpt.com/", "revision": 1}

    def test_two_spaced_identity_reads_are_required_on_a_stable_home(self):
        self.assertTrue(self.gate.observe(self.snapshot))
        self.assertFalse(self.gate.confirm_identity())
        self.now += 1.99
        self.assertFalse(self.gate.confirm_identity())
        self.assertEqual(self.gate.confirmations, 1)
        self.now += 0.02
        self.assertTrue(self.gate.confirm_identity())
        self.assertEqual(self.gate.confirmations, 2)

    def test_time_alone_does_not_replace_identity_confirmation(self):
        self.gate.observe(self.snapshot)
        self.now += 20
        self.assertFalse(self.gate.confirm_identity())

    def test_transient_loading_navigation_or_new_document_resets_confirmation(self):
        for change in ({"revision": 3}, {"document": 2}, {"url": "https://chatgpt.com/new"}):
            with self.subTest(change=change):
                self.setUp()
                self.gate.observe(self.snapshot)
                self.gate.confirm_identity()
                self.now += 3
                self.gate.observe({**self.snapshot, **change})
                self.assertFalse(self.gate.confirm_identity())
                self.now += 2
                self.assertTrue(self.gate.confirm_identity())

    def test_unknown_blocked_or_missing_world_never_passes(self):
        for blocked in ({}, {**self.snapshot, "ready": False}):
            self.gate.observe(self.snapshot)
            self.gate.confirm_identity()
            self.now += 3
            self.assertFalse(self.gate.observe(blocked))
            self.assertFalse(self.gate.confirm_identity())
            self.assertEqual(self.gate.diagnostics()["session_confirmations"], 0)

    def test_workspace_action_or_lost_identity_starts_fresh_interval(self):
        self.gate.observe(self.snapshot)
        self.gate.confirm_identity()
        self.now += 3
        self.gate.reset()
        self.gate.observe(self.snapshot)
        self.assertFalse(self.gate.confirm_identity())
        self.assertNotIn("chatgpt", json.dumps(self.gate.diagnostics()))


class ManagedSignupConfigurationTests(unittest.TestCase):
    def test_config_is_opt_in_and_rejects_unknown_mode(self):
        self.assertEqual(Settings(_env_file=None).browser_signup_flow, "legacy")
        self.assertEqual(Settings(_env_file=None, browser_signup_flow="extension").browser_signup_flow, "extension")
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, browser_signup_flow="unknown")

    def test_profile_is_stable_valid_and_corruption_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = signup_profile(directory)
            self.assertEqual(signup_profile(directory), profile)
            today, birth = date.today(), date.fromisoformat(profile["birthday"])
            age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))
            self.assertTrue(22 <= age <= 45)
            path = Path(directory) / ".team48-signup-profile.json"
            path.write_text("broken", encoding="utf-8")
            with self.assertRaises(ValueError):
                signup_profile(directory)
            self.assertEqual(path.read_text(encoding="utf-8"), "broken")

    def test_assets_share_content_and_container_excludes_private_config(self):
        content, version = signup_assets()
        self.assertIn(f"const VERSION = '{version}';", content)
        root = Path(__file__).resolve().parents[1]
        docker = (root / "deploy/Dockerfile").read_text(encoding="utf-8")
        self.assertIn("COPY extensions/chatgpt-signup/content.js extensions/chatgpt-signup/manifest.json", docker)
        self.assertNotIn("COPY extensions /", docker)
        self.assertIn("extensions/chatgpt-signup/private-config.mjs", (root / ".dockerignore").read_text())

    def test_navigation_failure_does_not_export_invitation_or_bridge_secrets(self):
        from app.integrations.openai.browser.signup import run_managed_signup
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            bridge = stack.enter_context(patch('app.integrations.openai.browser.signup.SignupBridge'))
            stack.enter_context(patch('app.integrations.mail.otp.list_mailbox_codes', return_value=[]))
            stack.enter_context(patch.object(onboard, 'goto_with_retries', side_effect=RuntimeError('https://chatgpt.com/accept-invite?token=PRIVATE_SECRET')))
            result = run_managed_signup(browser=MagicMock(), page=MagicMock(), email='test@icloud.com', password='secret',
                profile_dir=directory, start_url='https://chatgpt.com/accept-invite?token=PRIVATE_SECRET',
                invite_entry=True, team_name='', mail_kwargs={}, report=lambda *_: None)
            self.assertEqual(result['error_code'], 'registration_browser_failed')
            self.assertNotIn('PRIVATE_SECRET', json.dumps(result))
            bridge.return_value.close.assert_called_once()

    def test_new_runner_exclusively_owns_registration_and_hands_off_same_page(self):
        for succeeds in [True, False]:
            with self.subTest(succeeds=succeeds), ExitStack() as stack:
                browser, page, playwright = MagicMock(), MagicMock(), MagicMock()
                browser.pages = [page]
                playwright.chromium.launch_persistent_context.return_value = browser
                sync = stack.enter_context(patch("playwright.sync_api.sync_playwright"))
                sync.return_value.__enter__.return_value = playwright
                stack.enter_context(patch.object(onboard, "settings", Settings(_env_file=None, browser_signup_flow="extension")))
                stack.enter_context(patch.object(onboard, "Path"))
                stack.enter_context(patch.object(onboard, "chrome_proxy_launch"))
                stack.enter_context(patch.object(onboard, "chromium_context_kwargs", return_value={}))
                legacy = stack.enter_context(patch.object(onboard, "_fill_email"))
                navigation = stack.enter_context(patch.object(onboard, "goto_with_retries"))
                runner = stack.enter_context(patch("app.integrations.openai.browser.signup.run_managed_signup", return_value={"ok": succeeds, "error_code": "phone_verification_required" if not succeeds else ""}))
                def continued(result, live_browser, live_page):
                    self.assertIs(live_browser, browser)
                    self.assertIs(live_page, page)
                    browser.close.assert_not_called()
                    self.assertTrue(result["ok"])
                continuation = MagicMock(side_effect=continued)
                result = onboard.run_browser_onboard(email="test@icloud.com", password="secret",
                    proxy="socks5h://127.0.0.1:1080", start_url="https://chatgpt.com/accept-invite?token=fixture",
                    invite_entry=True, allow_sms=False, continue_in_browser=continuation)
                self.assertEqual(result["ok"], succeeds)
                self.assertEqual(runner.call_args.kwargs["start_url"], "https://chatgpt.com/accept-invite?token=fixture")
                legacy.assert_not_called()
                navigation.assert_not_called()
                self.assertEqual(continuation.call_count, int(succeeds))
                browser.close.assert_called_once()


class ManagedSignupPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_assets_prevent_hme_claim_or_invitation(self):
        from app.application.onboard import OnboardService
        settings = Settings(_env_file=None, browser_signup_flow="extension")
        with (patch("app.core.config.load_settings", return_value=settings),
              patch("app.integrations.openai.browser.signup.signup_assets", side_effect=FileNotFoundError),
              patch("app.application.onboard.hme_service.maybe_claim_alias", new_callable=AsyncMock) as claim,
              patch.object(OnboardService, "_invite_and_onboard_impl", new_callable=AsyncMock) as invite):
            result = await OnboardService().invite_and_onboard(None, workspace_id=1, email_line="", oauth_signup=True)
        self.assertEqual(result["error_code"], "browser_environment_invalid")
        claim.assert_not_called()
        invite.assert_not_called()


if __name__ == "__main__":
    unittest.main()
