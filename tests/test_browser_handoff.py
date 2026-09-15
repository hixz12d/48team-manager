"""Regression coverage for live invitation-to-OAuth browser handoff."""
import asyncio
from contextlib import ExitStack
from queue import Queue
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.application.jobs import browser as jobs
from app.integrations.openai.browser import onboard, reauth


class BrowserHandoffTests(unittest.TestCase):
    def worker(self, *, change=None, cancel=False, register_ok=True):
        events, commands = Queue(), Queue()
        context, page = MagicMock(), MagicMock()
        initial = {"email": "kid@example.com", "proxy": "socks5h://127.0.0.1:1080",
                   "executable_path": "chromix", "_invite_onboard": True}
        next_args = {"email": initial["email"], "proxy": initial["proxy"],
                     "executable_path": initial["executable_path"],
                     "authorize_url": "https://auth.openai.com/oauth/authorize",
                     "allow_signup": False}
        next_args.update(change or {})
        commands.put(None if cancel else next_args)

        def register(**kwargs):
            result = {"ok": register_ok}
            if register_ok:
                kwargs["continue_in_browser"](result, context, page)
            context.close()
            return result

        def authorize(**kwargs):
            context.close.assert_not_called()
            self.assertIs(kwargs["existing_browser"], context)
            self.assertIs(kwargs["existing_page"], page)
            self.assertFalse(kwargs["allow_signup"])
            return {"ok": True, "callback_url": "callback"}

        with (
            patch.object(onboard, "run_browser_onboard", side_effect=register),
            patch.object(reauth, "run_browser_oauth_reauth", side_effect=authorize) as oauth,
            patch("logging.disable"),
        ):
            jobs._reauth_process_main(events, initial, commands)
        results = []
        while not events.empty():
            event = events.get_nowait()
            if event["type"] == "result":
                results.append(event["result"])
        context.close.assert_called_once()
        return results, oauth

    def test_oauth_reuses_live_registration_context_and_page(self):
        results, oauth = self.worker()
        self.assertEqual(results, [{"ok": True}, {"ok": True, "callback_url": "callback"}])
        oauth.assert_called_once()

    def test_stop_after_membership_gate_closes_without_oauth(self):
        results, oauth = self.worker(cancel=True)
        self.assertEqual(results, [{"ok": True}])
        oauth.assert_not_called()

    def test_failed_registration_never_waits_for_oauth(self):
        results, oauth = self.worker(register_ok=False)
        self.assertEqual(results, [{"ok": False}])
        oauth.assert_not_called()

    def test_changed_identity_proxy_or_browser_cannot_reuse_context(self):
        for key in ("email", "proxy", "executable_path"):
            with self.subTest(key=key):
                results, oauth = self.worker(change={key: "changed"})
                self.assertEqual(results[-1]["error_code"], "browser_session_mismatch")
                oauth.assert_not_called()

    def test_reauth_uses_existing_page_without_launching_or_closing_browser(self):
        context, page = MagicMock(), MagicMock()
        callback = "http://localhost:1455/auth/callback?code=test&state=s"
        page.url = callback
        with ExitStack() as stack:
            launch = stack.enter_context(patch("playwright.sync_api.sync_playwright"))
            proxy = stack.enter_context(patch.object(reauth, "chrome_proxy_launch"))
            stack.enter_context(patch.object(reauth, "Path"))
            goto = stack.enter_context(patch.object(reauth, "goto_with_retries"))
            stack.enter_context(patch.object(reauth, "wait_cloudflare"))
            stack.enter_context(patch.object(reauth, "_snapshot_mailbox_codes", return_value=set()))
            result = reauth.run_browser_oauth_reauth(
                email="kid@example.com", password="password",
                proxy="socks5h://127.0.0.1:1080",
                authorize_url="https://auth.openai.com/oauth/authorize",
                allow_signup=False, allow_sms=False,
                existing_browser=context, existing_page=page,
            )
        self.assertTrue(result["ok"])
        self.assertEqual(result["callback_url"], callback)
        self.assertIs(goto.call_args.args[0], page)
        launch.assert_not_called()
        proxy.assert_not_called()
        context.new_page.assert_not_called()
        context.close.assert_not_called()

    def test_registration_handoff_runs_before_browser_closes(self):
        context, page, playwright = MagicMock(), MagicMock(), MagicMock()
        context.pages = [page]
        playwright.chromium.launch_persistent_context.return_value = context
        page.url = "https://chatgpt.com/"
        page.title.return_value = "ChatGPT"
        session = {"json": {"accessToken": "web-token", "user": {"email": "kid@example.com"}}}
        def continuation(result, live_context, live_page):
            self.assertTrue(result["ok"])
            self.assertIs(live_context, context)
            self.assertIs(live_page, page)
            context.close.assert_not_called()
        continued = MagicMock(side_effect=continuation)
        with ExitStack() as stack:
            sync = stack.enter_context(patch("playwright.sync_api.sync_playwright"))
            sync.return_value.__enter__.return_value = playwright
            proxy = stack.enter_context(patch.object(onboard, "chrome_proxy_launch"))
            proxy.return_value.__enter__.return_value = {}
            stack.enter_context(patch.object(onboard, "Path"))
            stack.enter_context(patch.object(onboard, "chromium_context_kwargs", return_value={}))
            stack.enter_context(patch.object(onboard, "wait_cloudflare", return_value=True))
            stack.enter_context(patch.object(onboard, "_peek_session", return_value=session))
            stack.enter_context(patch.object(onboard, "_page_text", return_value=""))
            stack.enter_context(patch.object(onboard, "_snapshot_mailbox_codes", return_value=set()))
            for name in ("goto_with_retries", "_save_debug"):
                stack.enter_context(patch.object(onboard, name))
            for name in ("_visible", "_email_input_visible", "_click_exact", "_find_otp",
                         "_otp_boxes", "_accept_terms", "_pick_workspace", "page_rate_limited"):
                stack.enter_context(patch.object(onboard, name, return_value=False))
            result = onboard.run_browser_onboard(
                email="kid@example.com", password="password", proxy="socks5h://127.0.0.1:1080",
                start_url="https://chatgpt.com/accept-invite?token=test", invite_entry=True,
                allow_sms=False, continue_in_browser=continued,
            )
        self.assertTrue(result["ok"])
        continued.assert_called_once()
        context.close.assert_called_once()


class SessionLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.lock = asyncio.Lock()
        self.lock_patch = patch.object(jobs, "_LOCK", self.lock)
        self.lock_patch.start()
        self.context = MagicMock()
        self.process = self.context.Process.return_value
        self.process.pid = 123
        self.process.is_alive.return_value = True
        self.events, self.commands = Queue(), MagicMock()
        self.events.cancel_join_thread = MagicMock()
        self.events.close = MagicMock()
        self.context.Queue.side_effect = [self.events, self.commands]
        self.spawn_patch = patch.object(jobs.multiprocessing, "get_context", return_value=self.context)
        self.spawn_patch.start()
        self.session = jobs.InvitedBrowserSession()

    async def asyncTearDown(self):
        await self.session.close()
        self.spawn_patch.stop()
        self.lock_patch.stop()

    async def test_slot_and_worker_are_held_between_stages(self):
        self.events.put({"type": "result", "result": {"ok": True}})
        await self.session.run(_invite_onboard=True, email="kid@example.com")
        self.assertTrue(self.lock.locked())
        self.process.join.assert_not_called()
        self.process.terminate.assert_not_called()
        self.events.put({"type": "result", "result": {"ok": True, "callback_url": "callback"}})
        result = await self.session.run(email="kid@example.com", authorize_url="oauth")
        self.assertEqual(result["callback_url"], "callback")
        self.context.Process.assert_called_once()
        self.commands.put.assert_called_once_with({"email": "kid@example.com", "authorize_url": "oauth"})
        self.process.is_alive.return_value = False
        await self.session.close()
        self.assertFalse(self.lock.locked())

    async def test_lost_session_is_not_restarted(self):
        self.events.put({"type": "result", "result": {"ok": True}})
        await self.session.run(_invite_onboard=True)
        self.process.is_alive.return_value = False
        result = await self.session.run(authorize_url="oauth")
        self.assertEqual(result["error_code"], "browser_session_lost")
        self.context.Process.assert_called_once()
        self.commands.put.assert_not_called()

    async def test_cancel_stops_worker_before_releasing_slot(self):
        self.events.put({"type": "stage", "stage": "browser_open"})
        callback = AsyncMock(side_effect=asyncio.CancelledError())
        with self.assertRaises(asyncio.CancelledError):
            try:
                await self.session.run(_invite_onboard=True, on_stage=callback)
            finally:
                await self.session.close()
        self.commands.put.assert_called_with(None)
        self.process.terminate.assert_called_once()
        self.process.kill.assert_called_once()
        self.assertFalse(self.lock.locked())
        self.process.is_alive.return_value = False

    async def test_start_failure_releases_slot(self):
        self.process.start.side_effect = OSError("cannot start worker")
        self.process.pid = None
        with self.assertRaises(OSError):
            try:
                await self.session.run(_invite_onboard=True)
            finally:
                await self.session.close()
        self.assertFalse(self.lock.locked())
        self.process.join.assert_not_called()


if __name__ == "__main__":
    unittest.main()
