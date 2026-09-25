"""Real unpacked extension, full multi-page signup fixture; no external account is created."""
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from playwright.sync_api import expect, sync_playwright
from tests.browser_signup_extension import EXTENSION, ROOT, mailbox_server


def wait_job(page, keys, expected, timeout=20000):
    # This Playwright build treats a Promise as truthy in wait_for_function.
    # Await the storage reads inside evaluate instead of passing it an async predicate.
    page.evaluate('''async ({keys,expected,timeout}) => {
        const deadline=Date.now()+timeout;
        while(Date.now()<deadline) {
            const {job}=await chrome.storage.session.get('job');
            const value=keys.reduce((object,key)=>object?.[key],job);
            if(expected===null ? value!=null : value===expected) return;
            await new Promise(resolve=>setTimeout(resolve,100));
        }
        throw new Error('Timed out waiting for job '+keys.join('.'));
    }''', {'keys':keys,'expected':expected,'timeout':timeout})


class SignupFlowTests(unittest.TestCase):
    def test_full_flow_with_zoom_pause_reload_and_resume(self):
        self.run_flow(skip_otp=False)

    def test_flow_without_otp_does_not_poll_or_submit_a_code(self):
        self.run_flow(skip_otp=True)

    def test_review_flow_waits_for_manual_submit_and_survives_reload(self):
        self.run_flow(skip_otp=False, mode='review')

    def test_stalled_homepage_recovers_via_login_and_finishes_review_flow(self):
        self.run_flow(skip_otp=False, mode='review', stalled_entry=True)

    def test_review_continue_retry_finishes_otp_and_profile(self):
        self.run_flow(skip_otp=False, mode='review', retry_continue=True)

    def test_auto_continue_retry_finishes_otp_and_profile(self):
        self.run_flow(skip_otp=False, retry_continue=True)

    def test_password_change_validation_enables_submit_after_blur(self):
        self.run_flow(skip_otp=True, blur_validation=True)

    def test_native_date_uses_keyboard_and_verified_birthday(self):
        self.run_flow(skip_otp=True, controls='date')

    def test_native_selects_use_keyboard_and_verified_birthday(self):
        self.run_flow(skip_otp=True, controls='select')

    def test_hidden_panel_popup_pause_and_resume_with_wheel_tab_enter(self):
        self.run_flow(skip_otp=False, hide_panel=True, scroll=True, keyboard=True)

    def test_homepage_login_modal_with_clipped_email_field(self):
        self.run_flow(skip_otp=False, modal=True)

    def run_flow(self, skip_otp, mode='auto', stalled_entry=False, retry_continue=False,
                 controls='age', hide_panel=False, scroll=False, keyboard=False, blur_validation=False, modal=False):
        deliver = threading.Event()
        with sync_playwright() as p, tempfile.TemporaryDirectory(prefix='team48-flow-') as tmp, mailbox_server(deliver) as (origin, requests):
            extension = Path(tmp) / 'extension'
            shutil.copytree(EXTENSION, extension, ignore=shutil.ignore_patterns('private-config.mjs'))
            (extension / 'private-config.mjs').write_text('export const MAILBOX = ' + json.dumps(
                {'baseUrl': origin, 'address': 'inbox@example.com', 'adminPassword': 'fixture-secret'}) + ';', encoding='utf-8')
            if keyboard:
                content = extension / 'content.js'
                content.write_text(content.read_text(encoding='utf-8').replace('Math.random() >= 0.4', 'false').replace('Math.random() < 0.35', 'true'), encoding='utf-8')
            mail = extension / 'cloudflare.mjs'
            mail.write_text(mail.read_text(encoding='utf-8').replace('https://apimail.xiaozhudf2026.foo', origin), encoding='utf-8')
            manifest = json.loads((extension / 'manifest.json').read_text(encoding='utf-8'))
            manifest['host_permissions'] = [origin + '/*']
            (extension / 'manifest.json').write_text(json.dumps(manifest), encoding='utf-8')
            context = p.chromium.launch_persistent_context(str(Path(tmp) / 'profile'), channel='chromium', headless=True,
                args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'],
                viewport={'width': 980, 'height': 800})
            try:
                errors = []
                context.on('page', lambda page: page.on('pageerror', lambda error: errors.append(str(error))))
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event('serviceworker')
                extension_id = worker.url.split('/')[2]
                admin = context.new_page()
                admin.goto('chrome://extensions/')
                admin.evaluate('() => chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode:true})')
                admin.evaluate('id => chrome.developerPrivate.updateExtensionConfiguration({extensionId:id,incognitoAccess:true})', extension_id)
                admin.wait_for_function('''async id => (await chrome.developerPrivate.getExtensionsInfo({includeDisabled:true}))
                    .some(x=>x.id===id && x.incognitoAccess.isActive)''', arg=extension_id)
                admin.goto(f'chrome-extension://{extension_id}/popup.html')
                with context.expect_page() as created:
                    admin.evaluate("chrome.windows.create({incognito:true,url:'about:blank'})")
                popup = created.value
                popup.goto(f'chrome-extension://{extension_id}/popup.html')
                html = (ROOT / 'tests/fixtures/signup_flow.html').read_text(encoding='utf-8')
                if controls != 'age':
                    old = "field('age','number','Age','min=\"18\" max=\"100\" required')"
                    if controls == 'date':
                        replacement = "field('birthday','date','Birthday','required')"
                        read_birthday = "f.birthday.value"
                    else:
                        selects = ''.join('<select name="'+part+'" required><option value="">Choose '+part+'</option>'+''.join('<option value="'+str(i)+'">'+str(i)+'</option>' for i in values)+'</select>' for part, values in [('year', range(1970, 2021)), ('month', range(1, 13)), ('day', range(1, 32))])
                        replacement = json.dumps(selects)
                        read_birthday = "f.year.value+'-'+f.month.value.padStart(2,'0')+'-'+f.day.value.padStart(2,'0')"
                    html = html.replace(old, replacement).replace('age:f.age.value', 'birthday:'+read_birthday)
                    html = html.replace("const old=event.target;const fresh", "const old=event.target;if(old.name!=='name')return;const fresh")
                if blur_validation:
                    html = html.replace(" f.onsubmit=e=>{e.preventDefault();save('password'", " f.querySelector('button').disabled=true;f.addEventListener('change',()=>{f.querySelector('button').disabled=false});\n f.onsubmit=e=>{e.preventDefault();save('password'")
                if scroll:
                    html = html.replace('margin:90px auto 160px', 'margin:950px auto 160px')
                if modal:
                    html = html.replace('const MODAL=false;', 'const MODAL=true;')
                if skip_otp:
                    html = html.replace("location.assign('/email-verification')", "location.assign('/about-you')")
                if retry_continue:
                    # Ignore the first click BEFORE any submit event occurs. A submit with
                    # no spinner now correctly waits instead of being clicked again.
                    for marker in [" f.code.focus();", " // A controlled field"]:
                        html = html.replace(marker, " f.querySelector('button').onclick=e=>{if(!window.ignoredContinue){window.ignoredContinue=true;e.preventDefault();}};\n"+marker)
                entry_requests = []
                def route(request):
                    if stalled_entry and request.request.url == 'https://chatgpt.com/':
                        request.fulfill(content_type='text/html', body='<html><body><button>Sign up</button></body></html>')
                    elif request.request.url == 'https://chatgpt.com/auth/login':
                        entry_requests.append(request.request.url)
                        # Playwright routing only intercepts the first request in an HTTP redirect chain.
                        # Use a separate page navigation so the auth fixture is intercepted as well.
                        request.fulfill(content_type='text/html', body='<html><script>location.replace("https://auth.openai.com/create-account")</script></html>')
                    elif request.request.url.endswith('/api/auth/session'):
                        request.fulfill(json={'user': {'email': 'test@icloud.com'}})
                    else:
                        request.fulfill(content_type='text/html', body=html)
                context.route('https://chatgpt.com/**', route)
                context.route('https://auth.openai.com/**', route)
                popup.locator('#email').fill('test@icloud.com')
                popup.locator('#workflow').select_option(mode)
                if hide_panel:
                    popup.locator('#hide-panel').check()
                with context.expect_page() as opened:
                    popup.locator('#start').click()
                registration = opened.value
                # Filling requires foreground focus; explicitly establish that fixture precondition.
                registration.bring_to_front()
                wait_job(popup, ['tabId'], None)
                if not modal:
                    registration.wait_for_url('https://auth.openai.com/create-account', timeout=25000 if stalled_entry else 15000)
                def wait_review(stage, path):
                    wait_job(popup, ['reviewSteps', stage+':'+path], True)
                if mode == 'review':
                    wait_review('email', '/create-account')
                    registration.reload()
                    expect(registration.locator('#team48-signup-progress')).to_have_attribute('data-version', manifest['version'])
                    expect(registration.locator('input[name=email]')).to_have_value('')
                    registration.locator('input[name=email]').fill('test@icloud.com')
                    registration.locator('form button').click()
                    registration.wait_for_url('**/create-account/password')
                    wait_review('password', '/create-account/password')
                    registration.locator('form button').click()
                    deliver.set()
                popup.evaluate("async () => {const {job}=await chrome.runtime.sendMessage({type:'view'});await chrome.tabs.setZoom(job.tabId,1.5)}")
                try:
                    registration.wait_for_url('https://auth.openai.com/' + ('about-you' if skip_otp else 'email-verification'), timeout=30000)
                except Exception as error:
                    state = popup.evaluate("async () => {const {job}=await chrome.runtime.sendMessage({type:'view'});return {status:job.status,message:job.message,mode:job.mode}}")
                    fields = registration.locator('input').evaluate_all("nodes=>nodes.map(el=>({type:el.type,length:el.value.length,valid:el.checkValidity()}))")
                    report = popup.evaluate("async () => (await chrome.runtime.sendMessage({type:'diagnostics'})).report")
                    raise AssertionError(json.dumps({'url':registration.url,'state':state,'fields':fields,'report':report}, ensure_ascii=False)) from error
                if hide_panel:
                    expect(registration.locator('#team48-signup-progress')).to_have_count(0)
                else:
                    expect(registration.locator('#team48-signup-progress')).to_have_attribute('data-version', manifest['version'])
                if not modal:  # Chrome zoom is per origin; the modal flow set it on chatgpt.com.
                    self.assertEqual(popup.evaluate("async () => {const {job}=await chrome.runtime.sendMessage({type:'view'});return chrome.tabs.getZoom(job.tabId)}"), 1.5)
                # Click the real pause button in the closed extension shadow root via its actual box.
                profile = popup.evaluate("async () => (await chrome.storage.session.get('job')).job.profile")
                if mode == 'review':
                    wait_review('otp', '/email-verification')
                    registration.locator('form button').click()
                    registration.wait_for_url('**/about-you')
                elif not skip_otp:
                    cdp = context.new_cdp_session(registration)
                    def find_action(node):
                        attrs = node.get('attributes', [])
                        if any(attrs[index:index+2] == ['id', 'action'] for index in range(0, len(attrs), 2)):
                            return node['nodeId']
                        for child in node.get('children', []) + node.get('shadowRoots', []):
                            found = find_action(child)
                            if found:
                                return found
                        return None
                    action = find_action(cdp.send('DOM.getDocument', {'depth': -1, 'pierce': True})['root'])
                    if hide_panel:
                        self.assertIsNone(action)
                        popup.locator('#pause').click()
                    else:
                        self.assertIsNotNone(action)
                        quad = cdp.send('DOM.getBoxModel', {'nodeId': action})['model']['content']
                        registration.mouse.click((quad[0]+quad[4])/2, (quad[1]+quad[5])/2)
                    wait_job(popup, ['status'], 'paused')
                    registration.reload()
                    if hide_panel:
                        expect(registration.locator('#team48-signup-progress')).to_have_count(0)
                    else:
                        expect(registration.locator('#team48-signup-progress')).to_have_attribute('data-version', manifest['version'])
                    self.assertEqual(registration.locator('input[name=code]').input_value(), '')
                    deliver.set()
                    if hide_panel:
                        popup.locator('#resume').click()
                    else:
                        popup.evaluate("chrome.runtime.sendMessage({type:'resume'})")
                    registration.wait_for_url('https://auth.openai.com/about-you', timeout=20000)
                email = registration.evaluate("sessionStorage.getItem('email')")
                password = registration.evaluate("sessionStorage.getItem('password')")
                self.assertEqual(email, 'test@icloud.com')
                self.assertGreater(len(password), 20)
                self.assertEqual(registration.evaluate("sessionStorage.getItem('code')"), None if skip_otp else '005239')
                # Review/reload deliberately uses a manual fill for the email; only automatic fields type character by character.
                for key in (['passwordTrace'] if mode == 'review' else ['emailTrace', 'passwordTrace']) + ([] if skip_otp else ['codeTrace']):
                    trace = registration.evaluate('key => JSON.parse(sessionStorage.getItem(key))', key)
                    self.assertTrue(trace)
                    if key == 'passwordTrace':
                        self.assertEqual(len(trace), 1)
                        self.assertEqual(trace[0]['data'], password)
                    else:
                        self.assertTrue(all(event['type'] == 'input' and len(event['data']) == 1 for event in trace))
                    # Browser-level (CDP) input: the page sees trusted events, not script-made ones.
                    self.assertTrue(all(event['trusted'] for event in trace), key)
                clicks = registration.evaluate("JSON.parse(sessionStorage.getItem('clicks') || '[]')")
                self.assertTrue(clicks and all(clicks), clicks)
                if mode == 'review':
                    wait_review('profile', '/about-you')
                    registration.locator('form button').click()
                try:
                    registration.wait_for_url('https://chatgpt.com/done', timeout=30000)
                except Exception as error:
                    state = popup.evaluate("async () => {const {job}=await chrome.runtime.sendMessage({type:'view'});return {status:job.status,message:job.message}}")
                    fields = registration.locator('input,select').evaluate_all("nodes=>nodes.map(el=>({name:el.name,type:el.type,length:el.value.length,valid:el.checkValidity()}))")
                    raise AssertionError(json.dumps({'url':registration.url,'state':state,'fields':fields}, ensure_ascii=True)) from error
                wait_job(popup, ['status'], 'done', timeout=15000)
                job = popup.evaluate("async () => (await chrome.storage.session.get('job')).job")
                self.assertEqual(job['profile'], profile)
                expected_attempts = {'signup':1,'email':1,'password':1,'profile':1}
                if not skip_otp:
                    expected_attempts['otp'] = 1
                if modal:
                    del expected_attempts['signup']  # The homepage dialog already shows the email field.
                if mode == 'review':
                    expected_attempts = {'signup': 1}
                if retry_continue:
                    for stage in ['otp', 'profile']:
                        expected_attempts[stage] = expected_attempts.get(stage, 0) + 1
                    self.assertEqual(job['continueRetries'], {'otp': 1, 'profile': 1})
                self.assertEqual(job['attempts'], expected_attempts)
                popup.locator('#codex-result').select_option('phone_required')
                wait_job(popup, ['codexResult'], 'phone_required')
                report = popup.evaluate("async () => (await chrome.runtime.sendMessage({type:'diagnostics'})).report")
                self.assertEqual(report['mode'], mode)
                self.assertEqual(report['input'], 'cdp')
                self.assertEqual(sum(event['event']=='continue_retry' for event in report['events']), 2 if retry_continue else 0)
                self.assertEqual(len(entry_requests), int(stalled_entry))
                self.assertEqual(sum(event['event']=='entry_fallback' for event in report['events']), int(stalled_entry))
                self.assertEqual(report['summary']['otpPageSeen'], not skip_otp)
                self.assertEqual(report['summary']['emailVerified'], 'unknown')
                self.assertNotIn('test@icloud.com', json.dumps(report))
                self.assertNotIn(password, json.dumps(report))
                if skip_otp:
                    self.assertEqual(len(requests), 1)  # Only the initial old-mail snapshot.
                # The original tab's auth storage is obtained by navigating back after completion.
                registration.goto('https://auth.openai.com/inspection')
                entered = registration.evaluate("JSON.parse(sessionStorage.getItem('profile'))")
                self.assertEqual(entered['name'], profile['name'])
                if controls == 'age':
                    self.assertTrue(22 <= int(entered['age']) <= 45)
                else:
                    self.assertEqual(entered['birthday'], profile['birthday'])
                    profile_trace = registration.evaluate("JSON.parse(sessionStorage.getItem('profileTrace'))")
                    self.assertTrue(profile_trace and all(event['trusted'] for event in profile_trace))
                if scroll:
                    wheel = registration.evaluate("JSON.parse(sessionStorage.getItem('wheel') || '[]')")
                    self.assertTrue(wheel and all(wheel))
                if keyboard:
                    keys = registration.evaluate("JSON.parse(sessionStorage.getItem('keys') || '[]')")
                    self.assertIn('Tab', keys)
                    self.assertIn('Enter', keys)
                if hide_panel:
                    self.assertTrue(job['hidePanel'])
                    expect(registration.locator('#team48-signup-progress')).to_have_count(0)
                self.assertTrue(all(item['valid'] for item in requests))
                self.assertFalse(errors, errors)
            finally:
                context.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
