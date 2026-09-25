"""Offline managed-runner integration: real Chromium, all HTTPS traffic routed locally."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from app.integrations.openai.browser.signup import run_managed_signup, SignupBridge, signup_assets
from app.integrations.openai.browser.signup_state import SignupState

ROOT = Path(__file__).resolve().parents[1]


class ManagedSignupBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def test_registration_and_invite_revisit_leave_clean_page_for_oauth(self):
        self._registration_roundtrip(invite_entry=True)

    def test_homepage_registration_accepts_pending_workspace_and_hands_off_clean_page(self):
        self._registration_roundtrip(invite_entry=False)

    def _registration_roundtrip(self, *, invite_entry):
        with tempfile.TemporaryDirectory(prefix="team48-managed-test-") as profile_dir:
            context = self.browser.new_context()
            self.addCleanup(context.close)
            page = context.new_page()
            html = (ROOT / "tests/fixtures/signup_flow.html").read_text(encoding="utf-8")
            # An ignored click has no submit event. A submitted form with no spinner
            # must now wait for its result instead of authorizing another click.
            for marker in [" f.code.focus();", " // A controlled field"]:
                html = html.replace(marker, " f.querySelector('button').onclick=e=>{if(!window.firstContinue){window.firstContinue=true;e.preventDefault();}};\n" + marker)
            finished, joined = False, False
            visits, reports = [], []

            def route(request):
                nonlocal finished, joined
                url = request.request.url
                visits.append(url)
                if url == "https://chatgpt.com/done":
                    finished = True
                if url.endswith("/api/auth/session"):
                    request.fulfill(json={"accessToken": "fixture-web-token", "user": {"email": "test@icloud.com", "id": "fixture-user"}} if finished else {})
                elif not invite_entry and url == "https://chatgpt.com/done" and not joined:
                    request.fulfill(content_type="text/html", body='<button onclick="fetch(\'/fixture/join\',{method:\'POST\'}).then(()=>location.reload())">Join workspace</button>')
                elif url == "https://chatgpt.com/accept-invite?token=fixture":
                    body = ('<button onclick="fetch(\'/fixture/join\',{method:\'POST\'}).then(()=>location.assign(\'/done\'))">Join workspace</button>'
                            if finished and not joined else '<script>location.replace("' + ("/done" if joined else "https://auth.openai.com/create-account") + '")</script>')
                    request.fulfill(content_type="text/html", body=body)
                elif url.endswith("/fixture/join"):
                    joined = True
                    request.fulfill(json={"ok": True})
                elif "/oauth/authorize" in url:
                    request.fulfill(content_type="text/html", body='<form><input name="email"><button type="button" onclick="window.authorized=true">Authorize</button></form>')
                else:
                    request.fulfill(content_type="text/html", body=html)

            context.route("**/*", route)
            count = 0
            def codes(**kwargs):
                nonlocal count
                count += 1
                self.assertEqual(kwargs["email"], "test@icloud.com")
                return ["123456"] if count == 1 else ["123456", "005239"]

            with patch("app.integrations.mail.otp.list_mailbox_codes", side_effect=codes):
                result = run_managed_signup(browser=context, page=page, email="test@icloud.com", password="TestPassword123!",
                    profile_dir=profile_dir, start_url="https://chatgpt.com/accept-invite?token=fixture" if invite_entry else "https://chatgpt.com/",
                    invite_entry=invite_entry, team_name="Fixture", mail_kwargs={"email": "test@icloud.com"},
                    report=lambda *args: reports.append(args))
            self.assertTrue(result["ok"], result)
            self.assertTrue(joined)
            self.assertEqual(result["signup_retries"], {"otp": 1, "profile": 1})
            self.assertEqual(visits.count("https://chatgpt.com/accept-invite?token=fixture"), 2 if invite_entry else 0)
            self.assertEqual(result["access_token"], "fixture-web-token")
            self.assertNotIn("TestPassword123!", json.dumps(reports))
            stages = [item[0] for item in reports]
            for stage in ('signup_workspace', 'signup_home_ready', 'signup_identity_confirmed', 'signup_ready', 'signup_runner_stopped'):
                self.assertIn(stage, stages)
            self.assertLess(stages.index('signup_workspace'), stages.index('signup_ready'))
            self.assertEqual(result['signup_diagnostics']['readiness']['session_confirmations'], 2)
            self.assertGreaterEqual(result['signup_diagnostics']['readiness']['stable_ms'], 2000)
            diagnostics = json.dumps(result['signup_diagnostics'])
            for secret in ('test@icloud.com', 'TestPassword123!', '005239', 'fixture-web-token', 'https://'):
                self.assertNotIn(secret, diagnostics)
            self.assertEqual(page.locator("#team48-signup-progress").count(), 0)
            self.assertTrue(page.evaluate("typeof __team48ManagedSignup === 'undefined'"))
            page.goto("https://auth.openai.com/oauth/authorize?fixture=true")
            page.wait_for_timeout(1800)
            self.assertEqual(page.locator("input").input_value(), "")
            self.assertEqual(page.locator("#team48-signup-progress").count(), 0)
            page.locator("button").click()
            self.assertTrue(page.evaluate("window.authorized"))
            page.goto("https://auth.openai.com/inspection")
            self.assertEqual(page.evaluate("sessionStorage.getItem('email')"), "test@icloud.com")
            self.assertEqual(page.evaluate("sessionStorage.getItem('code')"), "005239")

    def _observe_home(self, html, *, session=None, timeout=12):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        page = context.new_page()
        calls = []
        def route(request):
            if request.request.url.endswith('/api/auth/session'):
                calls.append(True)
                payload = session(len(calls)) if session else {'accessToken': 'fixture-token', 'user': {'email': 'test@icloud.com'}}
                request.fulfill(json=payload)
            else:
                request.fulfill(content_type='text/html', body=html)
        context.route('**/*', route)
        reports = []
        with tempfile.TemporaryDirectory() as directory, \
                patch('app.integrations.mail.otp.list_mailbox_codes', return_value=[]), \
                patch('app.integrations.openai.browser.signup.HOME_READY_TIMEOUT', timeout):
            result = run_managed_signup(browser=context, page=page, email='test@icloud.com', password='secret',
                profile_dir=directory, start_url='https://chatgpt.com/', invite_entry=False,
                team_name='Fixture', mail_kwargs={}, report=lambda *args: reports.append(args))
        return result, page, calls, reports

    def test_valid_session_on_unknown_page_does_not_finish_registration(self):
        result, _, _, reports = self._observe_home('<p>Finishing account setup</p>', timeout=0.6)
        self.assertEqual(result['error_code'], 'registration_home_not_ready')
        self.assertFalse(result['ok'])
        self.assertNotIn('signup_ready', [stage for stage, _ in reports])

    def test_ready_home_must_be_usable_and_clear_of_loading_and_dialogs(self):
        cases = [
            '<div id="prompt-textarea" contenteditable="false"></div>',
            '<div id="prompt-textarea" contenteditable="true" style="display:none"></div>',
            '<div id="prompt-textarea" contenteditable="true"></div><div role="dialog">Finish setup</div>',
            '<div id="prompt-textarea" contenteditable="true"></div><div aria-busy="true">Loading</div>',
        ]
        for html in cases:
            with self.subTest(html=html):
                result, _, _, _ = self._observe_home(html, timeout=0.6)
                self.assertEqual(result['error_code'], 'registration_home_not_ready')

    def test_short_loading_between_identity_reads_restarts_stability(self):
        html = '''<div id="prompt-textarea" contenteditable="true"></div><script>
        setTimeout(()=>{
          const busy=document.createElement('div');busy.setAttribute('role','progressbar');
          busy.textContent='Loading';document.body.append(busy);
          setTimeout(()=>{busy.remove();window.loadingEnded=performance.now()},250);
        },2200);
        </script>'''
        result, page, calls, _ = self._observe_home(html)
        self.assertTrue(result['ok'], result)
        self.assertTrue(page.evaluate('performance.now() - window.loadingEnded >= 2000'))
        self.assertGreaterEqual(len(calls), 4)
        self.assertTrue(page.evaluate('typeof __team48SignupReadiness === "undefined"'))

    def test_late_dialog_delays_handoff_until_it_is_gone(self):
        html = '''<div id="prompt-textarea" contenteditable="true"></div><script>
        setTimeout(()=>{
          const dialog=document.createElement('div');dialog.setAttribute('role','dialog');
          dialog.textContent='Setting up workspace';document.body.append(dialog);
          setTimeout(()=>{dialog.remove();window.dialogEnded=performance.now()},2200);
        },400);
        </script>'''
        result, page, _, _ = self._observe_home(html)
        self.assertTrue(result['ok'], result)
        self.assertTrue(page.evaluate('performance.now() - window.dialogEnded >= 2000'))

    def test_return_to_form_does_not_use_up_home_completion_timeout(self):
        html = '''<div id="prompt-textarea" contenteditable="true"></div><script>
        setTimeout(()=>{
          const field=document.createElement('input');field.type='email';field.disabled=true;
          document.body.append(field);
          setTimeout(()=>{field.remove();window.formEnded=performance.now()},5200);
        },300);
        </script>'''
        result, page, _, _ = self._observe_home(html, timeout=4.5)
        self.assertTrue(result['ok'], result)
        self.assertTrue(page.evaluate('performance.now() - window.formEnded >= 2000'))

    def test_identity_change_on_ready_home_never_hands_off(self):
        result, _, _, _ = self._observe_home('<div id="prompt-textarea" contenteditable="true"></div>',
            session=lambda count: {'accessToken': 'fixture-token', 'user': {'email': 'test@icloud.com' if count == 1 else 'wrong@icloud.com'}})
        self.assertEqual(result['error_code'], 'token_identity_mismatch')

    def test_unverified_session_never_hands_off(self):
        result, _, _, _ = self._observe_home('<div id="prompt-textarea" contenteditable="true"></div>',
            session=lambda _: {'accessToken': 'fixture-token', 'user': {'email': 'test@icloud.com', 'emailVerified': False}})
        self.assertEqual(result['error_code'], 'email_unverified')

    def test_wrong_session_email_cannot_accept_invitation(self):
        with tempfile.TemporaryDirectory(prefix="team48-managed-identity-") as profile_dir:
            context = self.browser.new_context()
            self.addCleanup(context.close)
            def route(request):
                if request.request.url.endswith('/api/auth/session'):
                    request.fulfill(json={'accessToken': 'fixture-token', 'user': {'email': 'wrong@icloud.com'}})
                else:
                    request.fulfill(content_type='text/html', body='<button onclick="window.joined=true">Join workspace</button>')
            context.route('**/*', route)
            page = context.new_page()
            with patch('app.integrations.mail.otp.list_mailbox_codes', return_value=[]):
                result = run_managed_signup(browser=context, page=page, email='test@icloud.com', password='secret',
                    profile_dir=profile_dir, start_url='https://chatgpt.com/accept-invite?token=fixture',
                    invite_entry=True, team_name='Fixture', mail_kwargs={}, report=lambda *_: None)
            self.assertEqual(result['error_code'], 'token_identity_mismatch')
            self.assertFalse(page.evaluate('!!window.joined'))

    def test_stop_during_typing_prevents_further_edits_and_reinjection(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route('**/*', lambda request: request.fulfill(content_type='text/html', body='<form><input type=email name=email><button>Continue</button></form>'))
        page = context.new_page()
        content, version = signup_assets()
        state = SignupState(email='test@icloud.com', password='secret', profile={'name': 'Test User', 'birthday': '1996-01-01'}, version=version, read_codes=lambda: [])
        bridge = SignupBridge(page, state, content)
        page.goto('https://auth.openai.com/create-account')
        for _ in range(200):
            page.wait_for_timeout(20)
            bridge.pump()
            if len(page.locator('input').input_value()) >= 2:
                break
        typed = page.locator('input').input_value()
        self.assertTrue(typed)
        self.assertLess(len(typed), len(state.email))
        bridge.close()
        stopped_value = page.locator('input').input_value()
        page.wait_for_timeout(1700)
        self.assertEqual(page.locator('input').input_value(), stopped_value)
        self.assertEqual(page.locator('#team48-signup-progress').count(), 0)
        page.reload()
        page.wait_for_timeout(1700)
        self.assertEqual(page.locator('input').input_value(), '')
        self.assertEqual(page.locator('#team48-signup-progress').count(), 0)

    def test_phone_stage_stops_without_any_sms_or_sensitive_diagnostics(self):
        with tempfile.TemporaryDirectory(prefix="team48-managed-phone-") as profile_dir:
            context = self.browser.new_context()
            self.addCleanup(context.close)
            context.route("**/*", lambda request: request.fulfill(content_type="text/html", body='<input type="tel" name="phone"><button onclick="window.sent=true">Send code</button>'))
            page = context.new_page()
            reports = []
            with patch("app.integrations.mail.otp.list_mailbox_codes", return_value=[]):
                result = run_managed_signup(browser=context, page=page, email="test@icloud.com", password="secret",
                    profile_dir=profile_dir, start_url="https://auth.openai.com/add-phone", invite_entry=False,
                    team_name="", mail_kwargs={}, report=lambda *args: reports.append(args))
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_code"], "phone_verification_required")
            self.assertFalse(page.evaluate("!!window.sent"))
            self.assertNotIn("secret", json.dumps(reports))

    def test_binding_is_not_accessible_to_main_world_iframes_or_external_hosts(self):
        context = self.browser.new_context()
        self.addCleanup(context.close)
        context.route("**/*", lambda request: request.fulfill(content_type="text/html", body='<p>Unknown page</p><iframe src="https://auth.openai.com/frame"></iframe>' if request.request.url.endswith('/safe') else '<p>Frame</p>'))
        page = context.new_page()
        content, version = signup_assets()
        state = SignupState(email="test@icloud.com", password="secret", profile={"name": "Test User", "birthday": "1996-01-01"}, version=version, read_codes=lambda: [])
        bridge = SignupBridge(page, state, content)
        try:
            page.goto("https://auth.openai.com/safe")
            for _ in range(20):
                page.wait_for_timeout(50)
                bridge.pump()
            self.assertTrue(any(event['event'] == 'page' for event in state.events))
            self.assertEqual(page.locator("#team48-signup-progress").count(), 1)
            self.assertEqual(page.evaluate("name=>typeof globalThis[name]", bridge.binding), "undefined")
            self.assertTrue(page.evaluate("typeof __team48ManagedSignup === 'undefined'"))
            self.assertEqual(page.frames[1].evaluate("name=>typeof globalThis[name]", bridge.binding), "undefined")
            self.assertEqual(page.frames[1].locator("#team48-signup-progress").count(), 0)
            page.goto("https://untrusted.invalid/safe")
            page.wait_for_timeout(100)
            bridge.pump()
            self.assertEqual(page.locator("#team48-signup-progress").count(), 0)
        finally:
            bridge.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
