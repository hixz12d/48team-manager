"""Privacy checks plus a disposable, mocked-site browser recording test.

No real account, HubStudio profile, mail, invite or OpenAI server is touched.
"""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.hubstudio_record import Recorder, dom_event, plugin_event, route


class SanitizationTests(unittest.TestCase):
    def test_routes_drop_identity_queries_fragments_and_tokens(self):
        values = [
            'https://chatgpt.com/backend-api/accounts/SECRET/users/SECRET?email=SECRET#SECRET',
            'https://chatgpt.com/backend-api/accounts/SECRET/invites',
            'https://auth.openai.com/authorize?state=SECRET&code_challenge=SECRET&login_hint=SECRET',
            'http://localhost:1455/auth/callback?code=SECRET&state=SECRET',
            'https://chatgpt.com/SECRET%40icloud.com/SECRET',
        ]
        for value in values:
            with self.subTest(url=value):
                result = route(value)
                self.assertIsNotNone(result)
                self.assertNotIn('SECRET', json.dumps(result))
        for value in ['https://chatgpt.com.attacker.test/', 'https://mail.example/SECRET', 'data:text/plain,SECRET']:
            self.assertIsNone(route(value))
        self.assertEqual(route(values[0])['route'], 'member')
        self.assertEqual(route(values[3])['route'], 'oauth_callback')

    def test_observer_payloads_are_allowlisted_again_in_python(self):
        output = dom_event({'kind': 'click', 'label': 'SECRET', 'value': 'SECRET',
            'password': 'SECRET', 'page': 'SECRET', 'fields': ['email', 'SECRET'], 'trusted': 'SECRET'})
        self.assertEqual(output, {'kind': 'click', 'page': 'other', 'fields': ['email']})
        output = plugin_event({'event': 'code_received', 'ms': 321, 'stage': 'otp', 'code': 'SECRET', 'email': 'SECRET'})
        self.assertEqual(output, {'event': 'code_received', 'ms': 321, 'stage': 'otp'})
        self.assertIsNone(plugin_event({'event': 'SECRET'}))


class BrowserRecordingTests(unittest.IsolatedAsyncioTestCase):
    async def test_record_manual_flow_without_secrets_or_page_actions(self):
        from playwright.async_api import async_playwright
        secret = 'SENTINEL_PASSWORD_TOKEN_728491'
        with tempfile.TemporaryDirectory(prefix='team48-recorder-test-') as tmp:
            root = Path(tmp)
            async with async_playwright() as playwright:
                context = await playwright.chromium.launch_persistent_context(
                    str(root / 'profile'), headless=True,
                    args=['--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1', '--disable-background-networking'])
                recorder = task = None
                try:
                    async def mock(request_route):
                        if '/backend-api/' in request_route.request.url:
                            await request_route.fulfill(status=200, json={'success': True, 'access_token': secret})
                            return
                        await request_route.fulfill(content_type='text/html', body=f'''<!doctype html>
                          <title>Disposable test</title><h1>Members</h1>
                          <input type=email value="{secret}"><input type=password value="{secret}">
                          <button id=remove>Remove member</button><button id=invite>Invite</button>
                          <button id=unknown>{secret}</button>
                          <script>
                            window.appClicks=0;
                            document.querySelector('#remove').onclick=()=>{{appClicks++;fetch('/backend-api/accounts/{secret}/users/{secret}',{{method:'DELETE',headers:{{Authorization:'Bearer {secret}'}}}})}};
                            document.querySelector('#invite').onclick=()=>{{appClicks++;fetch('/backend-api/accounts/{secret}/invites?token={secret}',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{email:'{secret}',password:'{secret}',code:'{secret}'}})}})}};
                          </script>''')
                    await context.route('**/*', mock)
                    port = int((root / 'profile/DevToolsActivePort').read_text().splitlines()[0])
                    page = context.pages[0]
                    await page.goto('https://chatgpt.com/admin/members')
                    recorder = Recorder(root / 'recording')
                    task = asyncio.create_task(recorder.record([port], 'nigimejknaadfcbpegojdnajfpomegcg', 20))
                    await page.wait_for_function('!!window.__team48FlowObserverV1', timeout=10000)
                    (recorder.folder / 'stage.txt').write_text('remove', encoding='utf-8')
                    for _ in range(20):
                        if recorder.stage == 'remove': break
                        await asyncio.sleep(.1)
                    self.assertEqual(recorder.stage, 'remove')
                    self.assertEqual(await page.evaluate('window.appClicks'), 0)
                    await page.locator('input[type=email]').fill(secret)
                    await page.locator('#remove').click()
                    await page.locator('#invite').click()
                    await page.locator('#unknown').click()
                    await page.wait_for_timeout(300)
                    # A newly created tab must be discovered and observed too.
                    oauth = await context.new_page()
                    await oauth.goto('https://auth.openai.com/add-phone?state='+secret)
                    await oauth.wait_for_function('!!window.__team48FlowObserverV1', timeout=10000)
                    await oauth.locator('h1').evaluate('(e)=>e.textContent="Phone number required"')
                    await oauth.wait_for_timeout(1200)
                    (recorder.folder / 'STOP').write_text('stop', encoding='utf-8')
                    await asyncio.wait_for(task, 15)
                    self.assertFalse(await page.evaluate('!!window.__team48FlowObserverV1'))
                    self.assertFalse(await oauth.evaluate('!!window.__team48FlowObserverV1'))
                    self.assertEqual(await page.evaluate('window.appClicks'), 2)
                    self.assertEqual(await page.title(), 'Disposable test')
                    text = (recorder.folder / 'timeline.jsonl').read_text(encoding='utf-8')
                    self.assertNotIn(secret, text)
                    self.assertNotIn('Authorization', text)
                    events = [json.loads(line) for line in text.splitlines()]
                    self.assertTrue(any(e['event']=='request' and e['route']=='member' and e['method']=='DELETE' for e in events))
                    self.assertTrue(any(e['event']=='response' and e['route']=='invites' and e['status']==200 for e in events))
                    self.assertTrue(any(e['event']=='ui' and e.get('label')=='remove' for e in events))
                    self.assertTrue(any(e['event']=='ui' and e.get('phone') is True for e in events))
                    self.assertTrue(any(e['event']=='ui' and e.get('label')=='other' for e in events))
                    self.assertEqual(json.loads((recorder.folder/'summary.json').read_text())['observer_errors'], 0)
                finally:
                    if recorder and task and not task.done():
                        recorder.stop.set()
                        await asyncio.gather(task, return_exceptions=True)
                    await context.close()


if __name__ == '__main__':
    unittest.main()
