"""Load the real extension with fixture credentials and a local Cloudflare-shaped API.

No project backend, real mailbox or real OpenAI account is used by these tests.
"""
import json
import shutil
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / "extensions" / "chatgpt-signup"
OUTPUT = ROOT / "dist"
VERSION = json.loads((EXTENSION / "manifest.json").read_text(encoding="utf-8"))["version"]


MOCK_RUNTIME = """
window.testMessages=[];
window.testClaims={};
window.testReservations={};
window.testContinueClicks={};window.testContinueRetries={};window.retryReservations={};
window.inputEvents=[];
window.mouseEvents=[];
for(const type of ['pointerover','mouseover','pointerenter','mouseenter','pointermove','mousemove',
 'pointerdown','mousedown','pointerup','mouseup','click']) {
 document.addEventListener(type,event=>{
  if(event.target.matches?.('input,button,a')) mouseEvents.push({type,tag:event.target.tagName,
   name:event.target.name,buttons:event.buttons,x:event.clientX,y:event.clientY});
 },true);
}
for(const type of ['keydown','keypress','beforeinput','input','keyup','paste']) {
 document.addEventListener(type,event=>{
  if(event.target instanceof HTMLInputElement) inputEvents.push({type,name:event.target.name,
   value:event.target.value,data:event.data,inputType:event.inputType,key:event.key});
 });
}
window.testState={ok:true,active:true,id:'fixture',email:'test@icloud.com',password:'ExampleOnly!123456',
 profile:{name:'Test User',birthday:'1990-03-02'},status:'running',message:'正在填写',stage:'email'};
window.chrome={runtime:{sendMessage:async message=>{
 testMessages.push(message);
 if(message.type==='state')return structuredClone(testState);
 if(message.type==='claim'){
  if(testClaims[message.stage]) return {ok:true,granted:false};
  if(!message.prepare) testClaims[message.stage]=true;
  if(message.reserve){const token=crypto.randomUUID();testReservations[message.stage]=token;return {ok:true,granted:true,token};}
  return {ok:true,granted:true};
 }
 if(message.type==='finish-click'){
  if(testReservations[message.stage]!==message.token)return {ok:true,accepted:false};
  delete testReservations[message.stage];
  if(!message.sent){
   if(retryReservations[message.stage])testContinueRetries[message.stage]--;else delete testClaims[message.stage];
  }else testContinueClicks[message.stage]={token:message.token,at:Date.now()};
  delete retryReservations[message.stage];
  return {ok:true,accepted:true};
 }
 if(message.type==='continue-observed'){
  const token=crypto.randomUUID();testContinueClicks[message.stage]={token,at:Date.now()};return {ok:true,token};
 }
 if(message.type==='retry-continue'){
  const previous=testContinueClicks[message.stage];
  if(!previous || previous.token!==message.token || (testContinueRetries[message.stage]||0)>=2)return {ok:true,granted:false};
  if(Date.now()-previous.at<2000)return {ok:true,granted:false,wait:true};
  testContinueRetries[message.stage]=(testContinueRetries[message.stage]||0)+1;
  const token=crypto.randomUUID();testReservations[message.stage]=token;retryReservations[message.stage]=true;
  return {ok:true,granted:true,token};
 }
 if(message.type==='review-ready'){testState.reviewSteps||={};testState.reviewSteps[message.stage+':'+location.pathname]=true;}
 if(message.type==='event' && message.event==='page' && ['email','password','otp','profile'].includes(message.stage)) testState.entryFormSeen=true;
 if(message.type==='entry-fallback'){
  if(testState.entryFallbackUsed || testState.entryFormSeen)return {ok:true,granted:false};
  testState.entryFallbackUsed=true;return {ok:true,granted:true,url:'https://chatgpt.com/auth/login'};
 }
 if(message.type==='code')return {ok:true,code:'654321'};
 if(message.type==='pause'){testState.active=false;testState.status='paused';testState.message=message.reason;}
 if(message.type==='complete'){testState.active=false;testState.status='done';}
 return {ok:true};
}}};
"""


@contextmanager
def mailbox_server(deliver=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            valid = (urlsplit(self.path).path == "/admin/mails" and self.headers.get("x-admin-auth") == "fixture-secret"
                     and query.get("address") == ["inbox@example.com"] and not self.headers.get("Cookie"))
            requests.append({"valid": valid, "path": urlsplit(self.path).path})
            old = {"id": 1, "source": "noreply@openai.com", "address": "test@icloud.com", "subject": "Verification code: 123456"}
            new = {"id": 2, "raw": "From: ChatGPT <noreply_at_tm_openai_com_random@icloud.com>\r\nTo: inbox@example.com\r\n"
                   "X-ICLOUD-HME: p=test@icloud.com; f=inbox@example.com; r=to; s=noreply@tm.openai.com\r\nSubject: Your verification code\r\n"
                   "Content-Transfer-Encoding: quoted-printable\r\n\r\nVerification code: =30=30=35=32=33=39"}
            payload = {"results": [old] if len(requests) == 1 or (deliver is not None and not deliver.is_set()) else [old, new]}
            body = json.dumps(payload).encode()
            self.send_response(200 if valid else 403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class SignupExtensionBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUTPUT.mkdir(exist_ok=True)
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def fixture(self, html, url="https://auth.openai.com/create-account"):
        context = self.browser.new_context(viewport={"width": 1100, "height": 760})
        self.addCleanup(context.close)
        context.add_init_script(MOCK_RUNTIME)

        def route(request):
            if request.request.url.endswith("/api/auth/session"):
                request.fulfill(json={"user": {"email": "test@icloud.com"}})
            else:
                request.fulfill(content_type="text/html", body="<html><body>" + html + "</body></html>")
        context.route("**/*", route)
        page = context.new_page()
        page.goto(url)
        page.add_script_tag(path=str(EXTENSION / "content.js"))
        return page

    def test_otp_and_age_wait_one_second_before_validation_and_click(self):
        for field in ['code', 'age']:
            with self.subTest(field=field):
                prefix = '<input name="name" value="Test User">' if field == 'age' else ''
                page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">' + prefix +
                                    f'<input name="{field}" onchange="window.filledAt=performance.now();'
                                    'this.setCustomValidity(\'checking\');'
                                    'setTimeout(()=>this.setCustomValidity(\'\'),800)">'
                                    '<button onpointerdown="window.clickedAt=performance.now()">Continue</button></form>')
                page.wait_for_function("window.submitted || testState.status==='paused'")
                self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
                self.assertGreaterEqual(page.evaluate('clickedAt-filledAt'), 1000)
                self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 1)
                page.close()

    def test_delayed_validation_recovers_on_single_recheck(self):
        for field in ['code', 'age']:
            with self.subTest(field=field):
                prefix = '<input name="name" value="Test User">' if field == 'age' else ''
                page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">' + prefix +
                                    f'<input name="{field}" onchange="this.setCustomValidity(\'checking\');'
                                    'setTimeout(()=>this.setCustomValidity(\'\'),1700)">'
                                    '<button>Continue</button></form>')
                page.wait_for_function("window.submitted || testState.status==='paused'")
                self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
                self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='pause')"))
                page.close()

    def test_step_reuse_before_submit_keeps_original_field_binding(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.unexpected=true">'
                            '<input name="code"><button>Continue</button></form>'
                            '<script>const send=chrome.runtime.sendMessage;chrome.runtime.sendMessage=async m=>{'
                            'const r=await send(m);if(m.type==="event" && m.event==="filled") {'
                            'const input=document.querySelector("input");input.name="email";input.value="";'
                            'testState.mode="manual";window.advanced=true}return r};</script>')
        page.wait_for_function('window.advanced===true')
        calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>n+2", arg=calls)
        self.assertFalse(page.evaluate('!!window.unexpected'))
        self.assertFalse(page.evaluate('!!testClaims.otp'))

    def test_step_reuse_during_validation_recheck_cancels_old_submit(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.unexpected=true">'
                            '<input name="code" onchange="this.setCustomValidity(\'checking\');'
                            'setTimeout(()=>{this.name=\'email\';this.value=\'\';this.setCustomValidity(\'\');'
                            'testState.mode=\'manual\';window.advanced=true},1500)">'
                            '<button>Continue</button></form>')
        page.wait_for_function('window.advanced===true')
        calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>n+2", arg=calls)
        self.assertFalse(page.evaluate('!!window.unexpected'))
        self.assertFalse(page.evaluate('!!testClaims.otp'))
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='pause')"))

    def test_pause_or_focus_loss_during_settle_cancels_click(self):
        for change in ['testState.active=false;testState.status="paused"',
                       'Object.defineProperty(document,"hasFocus",{value:()=>false,configurable:true})']:
            with self.subTest(change=change):
                page = self.fixture('<form onsubmit="event.preventDefault();window.unexpected=true">'
                                    '<input name="code"><button>Continue</button></form>'
                                    '<script>document.querySelector("input").onchange=()=>setTimeout(()=>{'
                                    + change + ';window.interrupted=true},500)</script>')
                page.wait_for_function('window.interrupted===true')
                calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
                page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>n+1", arg=calls)
                self.assertFalse(page.evaluate('!!window.unexpected'))
                self.assertFalse(page.evaluate('!!testClaims.otp'))
                page.close()

    def test_delayed_otp_auto_advance_during_settle_never_clicks(self):
        page = self.fixture('<form onsubmit="event.preventDefault();this.innerHTML=\'<p>Done</p>\';window.advanced=true">'
                            '<input name="code" onchange="setTimeout(()=>this.form.requestSubmit(),700)">'
                            '<button>Continue</button></form>')
        page.wait_for_function('window.advanced===true')
        calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>n+1", arg=calls)
        self.assertFalse(page.evaluate('!!testClaims.otp'))
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 0)

    def test_stalled_homepage_uses_auth_login_after_waiting(self):
        for html in ['<button>Sign up</button>', '<button disabled>Sign up</button>', '<p>Loading</p>',
                     '<div id="prompt-textarea"></div><script>fetch=async()=>({ok:true,json:async()=>({})})</script>']:
            with self.subTest(html=html):
                page = self.fixture(html, url='https://chatgpt.com/')
                page.wait_for_function("testMessages.filter(m=>m.type==='state').length>=2")
                self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='entry-fallback')"))
                page.evaluate('window.realNow=Date.now;Date.now=()=>realNow()+16000')
                page.wait_for_url('https://chatgpt.com/auth/login', timeout=12000)
                page.close()

    def test_late_email_dialog_cancels_entry_fallback_navigation(self):
        page = self.fixture('<button>Sign up</button><script>testState.mode="review";'
                            'const originalSend=chrome.runtime.sendMessage;chrome.runtime.sendMessage=async message=>{'
                            'const result=await originalSend(message);'
                            'if(message.type==="entry-fallback")document.body.insertAdjacentHTML("beforeend",'
                            '\'<form><input name="email" type="email"><button>Continue</button></form>\');return result};</script>',
                            url='https://chatgpt.com/')
        page.wait_for_function('testClaims.signup===true')
        page.evaluate('window.realNow=Date.now;Date.now=()=>realNow()+16000')
        page.wait_for_function("testMessages.some(m=>m.type==='review-ready')", timeout=12000)
        self.assertEqual(page.url, 'https://chatgpt.com/')
        self.assertEqual(page.locator('input[name=email]').input_value(), 'test@icloud.com')
        self.assertEqual(page.evaluate("testMessages.filter(m=>m.type==='entry-fallback').length"), 1)

    def test_fallback_endpoint_timeout_pauses_without_redirect_loop(self):
        page = self.fixture('<p>Loading</p><script>testState.entryFallbackUsed=true</script>',
                            url='https://chatgpt.com/auth/login')
        page.wait_for_function("testMessages.filter(m=>m.type==='state').length>=2")
        page.evaluate('window.realNow=Date.now;Date.now=()=>realNow()+16000')
        page.wait_for_function("testState.status==='paused'")
        self.assertEqual(page.url, 'https://chatgpt.com/auth/login')
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='entry-fallback')"))
        self.assertIn('备用注册入口', page.evaluate('testState.message'))

    def test_fallback_does_not_interrupt_manual_modes_existing_forms_or_challenges(self):
        cases = [('<script>testState.mode="manual"</script><button>Sign up</button>', False),
                 ('<script>testState.mode="submit"</script><button>Sign up</button>', False),
                 ('<script>testState.entryFormSeen=true</script><p>Loading</p>', False),
                 ('<p>Verify you are human</p>', True), ('<p>Too many requests</p>', True),
                 ('<input autocomplete="tel" name="phone">', True)]
        for html, paused in cases:
            with self.subTest(html=html):
                page = self.fixture(html, url='https://chatgpt.com/')
                page.wait_for_function("testMessages.filter(m=>m.type==='state').length>=2")
                page.evaluate('window.realNow=Date.now;Date.now=()=>realNow()+16000')
                count = page.evaluate("testMessages.filter(m=>m.type==='state').length")
                page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+2", arg=count)
                self.assertEqual(page.url, 'https://chatgpt.com/')
                self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='entry-fallback')"))
                self.assertEqual(page.evaluate('testState.status'), 'paused' if paused else 'running')
                page.close()

    def test_email_semantics_and_active_dialog_are_detected(self):
        variants = [
            '<input type="text" autocomplete="email">',
            '<input type="text" name="username">',
            '<label for="identifier">Email address</label><input id="identifier" type="text">',
            '<input type="tel" name="identifier" aria-label="Email or phone number">',
            '<input name="email" type="email" readonly value="previous@example.com">'
            '<button type="button" onclick="window.wrongButton=true">Continue</button>'
            '<div role="dialog"><input name="username" type="text" autocomplete="email"><button>Continue</button></div>',
        ]
        for fields in variants:
            with self.subTest(fields=fields):
                page = self.fixture('<script>testState.email="tents.darns7d@example.com"</script>'
                                    '<form onsubmit="event.preventDefault();window.submitted=true">' + fields +
                                    '<button>Continue</button></form>')
                page.wait_for_function("window.submitted || testState.status==='paused' || (testMessages.filter(m=>m.type==='state').length>=4 && !testMessages.some(m=>m.type==='claim'))", timeout=15000)
                self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
                self.assertEqual(page.locator('input:not([readonly])').input_value(), 'tents.darns7d@example.com')
                self.assertFalse(page.evaluate('!!window.wrongButton'))
                if page.locator('input[readonly]').count():
                    self.assertEqual(page.locator('input[readonly]').input_value(), 'previous@example.com')
                page.close()

    def test_focused_email_receives_simulated_click_before_typing(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email" onpointerdown="window.clickedInput=true" '
                            'onbeforeinput="if(!window.clickedInput)event.preventDefault()">'
                            '<button>Continue</button></form><script>document.querySelector("input").focus()</script>')
        page.wait_for_function("window.submitted || testState.status==='paused'", timeout=15000)
        self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='INPUT').map(e=>e.type)"),
                         ['pointerover','mouseover','pointerenter','mouseenter','pointermove','mousemove',
                          'pointerdown','mousedown','pointerup','mouseup','click'])

    def test_click_indicator_is_visible_but_does_not_intercept_input(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email"><button>Continue</button></form>')
        marker = page.locator('[data-team48-click]')
        expect(marker).to_be_visible()
        self.assertEqual(marker.evaluate("el=>getComputedStyle(el).pointerEvents"), 'none')
        page.locator('body').screenshot(path=str(OUTPUT / 'signup-extension-click.png'))
        page.wait_for_function('window.submitted===true')
        expect(marker).to_have_count(0)
        self.assertEqual(page.locator('input').input_value(), 'test@icloud.com')

    def test_email_replaced_on_focus_retries_without_pausing(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email" onfocus="this.removeAttribute(\'onfocus\');'
                            'const fresh=this.cloneNode(true);this.replaceWith(fresh);fresh.focus()">'
                            '<button>Continue</button></form>')
        page.wait_for_function("window.submitted || testState.status==='paused'", timeout=15000)
        self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
        self.assertEqual(page.locator('input').input_value(), 'test@icloud.com')
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 1)

    def test_email_password_otp_and_profile_form_filling(self):
        cases = [
            ('<input name="email" type="email">', 'input[name=email]', 'test@icloud.com'),
            ('<input type="password" autocomplete="new-password">', 'input[type=password]', 'ExampleOnly!123456'),
            ('<input name="code" autocomplete="one-time-code">', 'input[name=code]', '654321'),
            ('<input maxlength="1">' * 6, 'input', None),
        ]
        for fields, selector, expected in cases:
            with self.subTest(fields=fields):
                page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">' + fields + '<button>Continue</button></form>')
                page.wait_for_function("window.submitted === true")
                if expected is None:
                    self.assertEqual(page.locator(selector).evaluate_all("nodes=>nodes.map(n=>n.value).join('')"), "654321")
                else:
                    self.assertEqual(page.locator(selector).input_value(), expected)
                events = page.evaluate("inputEvents")
                self.assertFalse(any(event['type'] == 'paste' for event in events))
                inserts = [event for event in events if event['type'] == 'input']
                self.assertTrue(inserts)
                self.assertTrue(all(event.get('inputType') == 'insertText' and len(event.get('data', '')) == 1 for event in inserts))
                self.assertFalse(any(event['type'] in ['keydown', 'keypress', 'keyup'] for event in events))
                if expected is not None:
                    values = page.locator(selector).evaluate("el => inputEvents.filter(e=>e.name===el.name && e.type==='input').map(e=>e.value)")
                    self.assertEqual(values, [expected[:index] for index in range(1, len(expected) + 1)])
                self.assertTrue(page.locator("#team48-signup-progress").is_visible())
                page.close()

    def test_filling_avoids_keyboard_handlers_and_redundant_focus(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'
                            '<input name="email" type="text"><button>Continue</button></form>'
                            '<script>window.focusCalls=0;const originalFocus=HTMLInputElement.prototype.focus;'
                            'HTMLInputElement.prototype.focus=function(...args){focusCalls++;return originalFocus.apply(this,args)};'
                            'document.querySelector("input").addEventListener("keydown",e=>e.target.form.requestSubmit());</script>')
        page.wait_for_function('window.submits===1')
        self.assertEqual(page.locator('input').input_value(), 'test@icloud.com')
        self.assertEqual(page.evaluate('focusCalls'), 1)
        self.assertFalse(page.evaluate("inputEvents.some(e=>['keydown','keypress','keyup'].includes(e.type))"))

    def test_review_auto_submit_stops_filling_without_another_prompt(self):
        cases = [
            ('<input name="code" autocomplete="one-time-code" oninput="if(this.value.length===6)this.form.requestSubmit()" onblur="window.blurred=true">', 'input[name=code]', '654321'),
            ('<input name="code" autocomplete="one-time-code" onchange="this.form.requestSubmit()" onblur="window.blurred=true">', 'input[name=code]', '654321'),
            ('<input name="name" oninput="if(this.value.length===3){this.form.setAttribute(\'aria-busy\',\'true\');this.form.requestSubmit()}" onblur="window.blurred=true"><input name="age" type="number">', 'input[name=name]', 'Tes'),
        ]
        for fields, selector, value in cases:
            with self.subTest(fields=fields):
                page = self.fixture('<script>testState.mode="review"</script>'
                                    '<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'+fields+'<button>Continue</button></form>')
                page.wait_for_function('window.submits===1')
                calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
                page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+3", arg=calls)
                self.assertEqual(page.locator(selector).input_value(), value)
                self.assertEqual(page.evaluate('window.submits'), 1)
                self.assertFalse(page.evaluate('!!window.blurred'))
                self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='review-ready')"))
                self.assertFalse(page.evaluate("mouseEvents.some(e=>e.tag==='BUTTON' && e.type==='click')"))
                if page.locator('input[name=age]').count():
                    self.assertEqual(page.locator('input[name=age]').input_value(), '')
                page.close()

    def test_age_label_and_incomplete_submit_hint_do_not_block_filling(self):
        for required in ['', 'required']:
            with self.subTest(required=required):
                page = self.fixture('<form onsubmit="event.preventDefault();if(event.isTrusted)window.submitted=true">'
                                    '<input name="name" onchange="this.form.dispatchEvent(new Event(\'submit\',{bubbles:true,cancelable:true}))">'
                                    f'<label for="years">Age</label><input id="years" type="number" {required} '
                                    'oninput="this.form.dispatchEvent(new Event(\'submit\',{bubbles:true,cancelable:true}))">'
                                    '<button>Continue</button></form>')
                page.wait_for_function("document.querySelector('#years').value.length===2 || testState.status==='paused'", timeout=15000)
                # Validation submits can occur per character; the age must still be complete.
                self.assertEqual(page.evaluate('testState.status'), 'running')
                self.assertEqual(page.locator('input[name=name]').input_value(), 'Test User')
                self.assertTrue(22 <= int(page.locator('#years').input_value()) <= 45)
                self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.name==='' && e.tag==='INPUT' && e.type==='click').length"), 1)
                page.close()

    def test_reused_otp_form_can_advance_to_profile_without_old_submit_lock(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="code" autocomplete="one-time-code" oninput="if(this.value.length===6){'
                            'const form=this.form;form.dispatchEvent(new Event(\'submit\',{bubbles:true,cancelable:true}));'
                            'setTimeout(()=>{window.submitted=false;form.innerHTML=\'<input name=&quot;name&quot; required><input name=&quot;age&quot; type=&quot;number&quot; required><button>Continue</button>\'},200)}">'
                            '<button>Continue</button></form>')
        page.wait_for_function("document.querySelector('input[name=age]')")
        page.wait_for_function("window.submitted && document.querySelector('input[name=age]').value", timeout=15000)
        self.assertEqual(page.locator('input[name=name]').input_value(), 'Test User')
        self.assertTrue(22 <= int(page.locator('input[name=age]').input_value()) <= 45)
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 1)

    def test_window_blur_waits_and_resumes_with_click_and_correct_prefix(self):
        # Deterministic OS window-focus signal; native input/blur and DOM edits still run in Chromium.
        page = self.fixture('<script>window.away=false;document.hasFocus=()=>!window.away;</script>'
                            '<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email" oninput="if(!window.leftOnce && this.value.length===3){'
                            'window.leftOnce=true;window.away=true;this.blur()}"><button>Continue</button></form>')
        page.wait_for_function('window.away')
        calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+3", arg=calls)
        self.assertEqual(page.locator('input').input_value(), 'tes')
        self.assertEqual(page.evaluate('testState.status'), 'running')
        self.assertFalse(page.evaluate('!!window.submitted'))
        page.evaluate('window.away=false')
        page.wait_for_function('window.submitted===true')
        self.assertEqual(page.locator('input').input_value(), 'test@icloud.com')
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='INPUT' && e.type==='click').length"), 2)
        values = page.evaluate("inputEvents.filter(e=>e.type==='input').map(e=>e.value)")
        self.assertEqual(values, ['test@icloud.com'[:i] for i in range(1, len('test@icloud.com')+1)])

    def test_lost_input_focus_is_recovered_by_click_before_next_character(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email" oninput="if(this.value.length===3){window.lostFocus=true;this.blur()}" '
                            'onpointerdown="window.lostFocus=false" onbeforeinput="if(window.lostFocus)event.preventDefault()">'
                            '<button>Continue</button></form>')
        page.wait_for_function("window.submitted || testState.status==='paused'", timeout=15000)
        self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
        self.assertEqual(page.locator('input').input_value(), 'test@icloud.com')
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='INPUT' && e.type==='click').length"), 2)

    def test_native_date_preserves_control_type_and_emits_only_valid_value(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="name"><input name="birthday" type="date" required><button>Continue</button></form>'
                            '<script>window.types=[];new MutationObserver(records=>types.push(...records.map(r=>r.oldValue)))'
                            '.observe(document.querySelector("input[type=date]"),{attributes:true,attributeFilter:["type"],attributeOldValue:true});</script>')
        page.wait_for_function('window.submitted===true')
        self.assertEqual(page.locator('input[type=date]').input_value(), '1990-03-02')
        self.assertEqual(page.evaluate('types'), [])
        self.assertEqual(page.evaluate("inputEvents.filter(e=>e.name==='birthday' && e.type==='input').map(e=>e.value)"), ['1990-03-02'])

    def test_review_preserves_manual_corrections_without_clicking_submit(self):
        page = self.fixture('<script>testState.mode="review"</script>'
                            '<form onsubmit="event.preventDefault();window.submitted=true"><input name="email" type="email">'
                            '<button>Continue</button></form>')
        page.wait_for_function("testMessages.some(m=>m.type==='review-ready')")
        self.assertEqual(page.locator('input').input_value(), 'test@icloud.com')
        self.assertFalse(page.evaluate('!!window.submitted'))
        page.locator('input').fill('corrected@example.com')
        calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+2", arg=calls)
        self.assertEqual(page.locator('input').input_value(), 'corrected@example.com')
        self.assertEqual(page.evaluate('testState.status'), 'running')
        self.assertFalse(page.evaluate("mouseEvents.some(e=>e.tag==='BUTTON' && e.type==='click')"))
        page.locator('form button').click()
        self.assertTrue(page.evaluate('window.submitted'))

    def test_manual_mode_never_fills_clicks_or_fetches_mail(self):
        page = self.fixture('<script>testState.mode="manual"</script>'
                            '<form><input name="code" autocomplete="one-time-code"><button>Continue</button></form>')
        page.wait_for_function("testMessages.filter(m=>m.type==='state').length>=3")
        self.assertEqual(page.locator('input').input_value(), '')
        self.assertEqual(page.evaluate('inputEvents.length+mouseEvents.length'), 0)
        self.assertFalse(page.evaluate("testMessages.some(m=>['code','claim'].includes(m.type))"))
        self.assertTrue(page.evaluate("testMessages.some(m=>m.type==='event' && m.stage==='otp')"))

    def click_panel_button(self, page, button_id):
        cdp = page.context.new_cdp_session(page)
        def find(node):
            attrs = node.get('attributes', [])
            if any(attrs[i:i+2] == ['id', button_id] for i in range(0, len(attrs), 2)):
                return node['nodeId']
            for child in node.get('children', []) + node.get('shadowRoots', []):
                result = find(child)
                if result:
                    return result
            return None
        node = find(cdp.send('DOM.getDocument', {'depth': -1, 'pierce': True})['root'])
        self.assertIsNotNone(node)
        quad = cdp.send('DOM.getBoxModel', {'nodeId': node})['model']['content']
        page.mouse.click((quad[0]+quad[4])/2, (quad[1]+quad[5])/2)
        cdp.detach()

    def test_submit_mode_requires_explicit_confirmation_and_keeps_manual_value(self):
        page = self.fixture('<script>testState.mode="submit"</script>'
                            '<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'
                            '<input type="password" autocomplete="new-password" required minlength="8"><button>Continue</button></form>')
        page.wait_for_function("testMessages.some(m=>m.event==='waiting_manual')")
        page.locator('input').fill('MyManualPassword!')
        calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+2", arg=calls)
        self.assertFalse(page.evaluate('!!window.submits'))
        self.click_panel_button(page, 'submit-step')
        page.wait_for_function('window.submits===1')
        self.assertEqual(page.locator('input').input_value(), 'MyManualPassword!')
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='code')"))

    def test_auto_submitting_otp_with_loading_is_not_submitted_twice(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'
                            '<input name="code" autocomplete="one-time-code" '
                            'oninput="if(this.value.length===6){this.form.setAttribute(\'aria-busy\',\'true\');this.form.requestSubmit()}">'
                            '<button>Continue</button></form>')
        page.wait_for_function('window.submits===1')
        page.wait_for_timeout(4500)
        self.assertEqual(page.evaluate('window.submits'), 1)
        self.assertFalse(page.evaluate("mouseEvents.some(e=>e.tag==='BUTTON' && e.type==='click')"))

    def test_idle_otp_auto_submit_gets_one_click_after_short_wait(self):
        page = self.fixture('<form onsubmit="event.preventDefault();(window.submitTimes||=[]).push(performance.now())">'
                            '<input name="code" autocomplete="one-time-code" '
                            'oninput="if(this.value.length===6)this.form.requestSubmit()">'
                            '<button onpointerdown="window.clickedAt=performance.now()">Continue</button></form>')
        page.wait_for_function('(window.submitTimes||[]).length===2', timeout=10000)
        self.assertGreaterEqual(page.evaluate('clickedAt-submitTimes[0]'), 2500)
        self.assertLess(page.evaluate('clickedAt-submitTimes[0]'), 4500)
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 1)
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='pause')"))

    def test_validation_submits_while_typing_click_one_second_after_filling(self):
        # Real OTP/about-you pages dispatch DOM submit on individual inputs; judge only after filling.
        submit = 'oninput="window.lastInput=performance.now();this.form.dispatchEvent(new Event(\'submit\',{bubbles:true,cancelable:true}))"'
        cases = [('otp', f'<input name="code" autocomplete="one-time-code" {submit}>'),
                 ('profile', f'<input name="name" {submit}><input name="age" type="number" {submit}>')]
        for stage, fields in cases:
            with self.subTest(stage=stage):
                page = self.fixture('<form onsubmit="event.preventDefault();if(event.submitter)window.submitted=true">' + fields +
                                    '<button onpointerdown="window.clickedAt=performance.now()">Continue</button></form>')
                page.wait_for_function("window.submitted || testState.status==='paused'", timeout=10000)
                self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
                waited = page.evaluate('clickedAt-lastInput')
                self.assertGreaterEqual(waited, 1000)
                self.assertLess(waited, 2600 if stage == 'profile' else 3500)
                self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 1)
                page.close()

    def test_submission_timeout_rechecks_worker_claim_before_pausing(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email"><button>Continue</button></form>')
        page.wait_for_function('window.submitted===true')
        index = page.evaluate('testMessages.length')
        page.evaluate('window.originalNow=Date.now;Date.now=()=>originalNow()+46000')
        page.wait_for_function("testState.status==='paused'")
        self.assertTrue(page.evaluate("index=>testMessages.slice(index).some(m=>m.type==='claim' && m.stage==='email' && m.prepare)", index))

    def test_invalid_form_and_manual_intervention_block_auto_submit(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email"><input name="terms" type="checkbox" required><button>Continue</button></form>')
        page.wait_for_function("testState.status==='paused'")
        self.assertFalse(page.evaluate('!!window.submitted'))
        self.assertFalse(page.evaluate('!!testClaims.email'))
        page.close()
        page = self.fixture('<form><input name="email" type="email"><button>Continue</button></form>')
        page.wait_for_function('document.querySelector("input").value.length>=2')
        page.locator('input').fill('manual@example.com')
        page.wait_for_function("testState.status==='paused'")
        self.assertEqual(page.locator('input').input_value(), 'manual@example.com')
        self.assertFalse(page.evaluate('!!testClaims.email'))

    def test_profile_waits_after_submit_and_typing_can_pause(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true;this.querySelectorAll(\'input,button\').forEach(el=>el.disabled=true)">'
                            '<input name="name"><input name="age" type="number"><button>Continue</button></form>')
        page.wait_for_function('window.submitted === true')
        typed = page.evaluate("inputEvents.filter(e=>e.type==='input').length")
        state_calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("count => testMessages.filter(m=>m.type==='state').length >= count + 3", arg=state_calls)
        self.assertEqual(page.evaluate("inputEvents.filter(e=>e.type==='input').length"), typed)
        self.assertEqual(page.evaluate('testState.status'), 'running')
        page.close()
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true"><input name="email" type="email" '
                            'oninput="if(!window.pausedOnce && this.value.length===3){window.pausedOnce=true;testState.active=false;testState.status=\'paused\'}">'
                            '<button>Continue</button></form>')
        page.wait_for_function("testState.status==='paused'")
        self.assertEqual(page.locator('input').input_value(), 'tes')
        self.assertFalse(page.evaluate('!!window.submitted'))
        self.assertFalse(page.evaluate('!!testClaims.email'))
        # Wait for the in-flight typing call to observe the pause before resuming.
        page.wait_for_function("testMessages.filter(m=>m.type==='state').length >= 10")
        page.evaluate("testState.active=true;testState.status='running'")
        page.wait_for_function('window.submitted === true')
        self.assertEqual(page.locator('input').input_value(), 'test@icloud.com')

    def test_age_loading_does_not_trigger_obstruction_or_refill(self):
        # Existing filled page / automatic submission on blur: spinner lasts beyond the old 5s click timeout.
        page = self.fixture('<form aria-busy="true" onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="name" value="Grace Carter" disabled><input name="age" value="21" disabled>'
                            '<button disabled><span class="animate-spin">Loading</span></button></form>')
        page.wait_for_function("testMessages.filter(m=>m.type==='state').length >= 6", timeout=15000)
        self.assertEqual(page.evaluate('testState.status'), 'running')
        self.assertFalse(page.evaluate('!!window.submitted'))
        self.assertEqual(page.evaluate('inputEvents.length'), 0)
        self.assertEqual(page.evaluate('mouseEvents.length'), 0)
        page.close()
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="name"><input name="age" type="number" '
                            'onblur="this.form.setAttribute(\'aria-busy\',\'true\');this.form.querySelector(\'button\').disabled=true">'
                            '<button>Continue</button></form>')
        page.wait_for_function("document.querySelector('form').getAttribute('aria-busy')==='true'")
        state_calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("count => testMessages.filter(m=>m.type==='state').length >= count + 5", arg=state_calls, timeout=15000)
        self.assertEqual(page.evaluate('testState.status'), 'running')
        self.assertFalse(page.evaluate('!!window.submitted'))
        self.assertFalse(page.evaluate('!!testClaims.profile'))
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 0)

    def test_age_is_corrected_before_submit_and_rejected_if_page_changes_it(self):
        for change_on_blur in [False, True]:
            with self.subTest(change_on_blur=change_on_blur):
                blur = 'onblur="this.value=21"' if change_on_blur else ''
                page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                                    f'<input name="name"><input name="age" type="number" value="21" {blur}>'
                                    '<button>Continue</button></form>')
                if change_on_blur:
                    page.wait_for_function("testState.status==='paused'")
                    self.assertFalse(page.evaluate('!!window.submitted'))
                    self.assertFalse(page.evaluate('!!testClaims.profile'))
                else:
                    page.wait_for_function('window.submitted===true')
                    expected_age = page.evaluate("""() => {
                        const now=new Date();return now.getFullYear()-1990-
                          (now.getMonth()<2 || (now.getMonth()===2 && now.getDate()<2) ? 1:0);
                    }""")
                    self.assertEqual(page.locator('input[name=age]').input_value(), str(expected_age))
                page.close()

    def test_combined_email_password_and_disabled_fieldset(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<fieldset disabled><input name="email" type="email" required>'
                            '<input type="password" autocomplete="new-password" required><button>Continue</button></fieldset></form>'
                            '<script>setTimeout(()=>document.querySelector("fieldset").disabled=false,1200)</script>')
        page.wait_for_function('window.submitted===true')
        self.assertEqual(page.locator('input[name=email]').input_value(), 'test@icloud.com')
        self.assertEqual(page.locator('input[type=password]').input_value(), 'ExampleOnly!123456')

    def test_otp_auto_advance_does_not_click_next_page_button(self):
        page = self.fixture('<form><input name="code" autocomplete="one-time-code" '
                            'oninput="if(this.value.length===6){window.code=this.value;this.form.outerHTML=\'<button onclick=&quot;window.unexpected=true&quot;>Continue</button>\'}">'
                            '<button>Continue</button></form>')
        page.wait_for_function("window.code==='654321'")
        state_calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("count=>testMessages.filter(m=>m.type==='state').length>count+1", arg=state_calls)
        self.assertFalse(page.evaluate('!!window.unexpected'))
        self.assertFalse(page.evaluate('!!testClaims.otp'))

    def test_extension_disconnect_stops_typing_and_shows_refresh(self):
        page = self.fixture('<input name="email" type="email" oninput="if(this.value.length===2)chrome.runtime.sendMessage=async()=>{throw new Error(\'Extension context invalidated\')}">')
        page.wait_for_function("document.querySelector('input').value.length===2")
        expect(page.locator('#team48-signup-progress')).to_have_attribute('data-version', VERSION)
        self.assertFalse(page.evaluate('!!testClaims.email'))
        self.assertEqual(page.locator('input').input_value(), 'te')

    def test_focused_otp_under_associated_label_is_typed(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<div style="position:relative;width:280px;height:60px">'
                            '<input id="otp" name="code" autocomplete="one-time-code" style="width:100%;height:100%">'
                            '<label for="otp" style="position:absolute;inset:0">Code</label></div>'
                            '<button>Continue</button></form><script>document.querySelector("input").focus()</script>')
        page.wait_for_function("window.submitted || testState.status==='paused'", timeout=15000)
        self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
        self.assertEqual(page.locator('input').input_value(), '654321')

    def test_replaced_inputs_preserve_incremental_typing(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="name"><input name="age" type="number"><button>Continue</button></form>'
                            '<script>document.addEventListener("input",event=>{'
                            'const clone=event.target.cloneNode(true);clone.value=event.target.value;'
                            'event.target.replaceWith(clone);clone.focus();});</script>')
        page.wait_for_function("window.submitted || testState.status==='paused'", timeout=15000)
        self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
        self.assertEqual(page.locator('input[name=name]').input_value(), 'Test User')
        self.assertEqual(page.evaluate("inputEvents.filter(e=>e.name==='name' && e.type==='input').map(e=>e.value)"),
                         ['T','Te','Tes','Test','Test ','Test U','Test Us','Test Use','Test User'])

    def test_duplicate_content_injection_only_submits_once(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'
                            '<input name="email" type="email"><button>Continue</button></form>')
        page.add_script_tag(path=str(EXTENSION / 'content.js'))
        page.wait_for_function("window.submits || testState.status==='paused'", timeout=15000)
        self.assertEqual(page.evaluate('window.submits'), 1)
        self.assertEqual(page.locator('#team48-signup-progress').count(), 1)

    def test_profile_submit_avoids_extension_panel(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="name"><input name="age" type="number">'
                            '<button style="position:fixed;right:60px;bottom:80px;width:160px;height:44px">Continue</button></form>')
        page.wait_for_function("window.submitted === true || testState.status === 'paused'", timeout=15000)
        self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 1)
        self.assertTrue(page.locator('#team48-signup-progress').is_visible())

    def test_profile_submit_uses_uncovered_part_of_button(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="name"><input name="age" type="number">'
                            '<div style="position:relative;width:300px;height:48px">'
                            '<button style="width:300px;height:48px">Continue</button>'
                            '<div style="position:absolute;left:120px;top:0;width:60px;height:48px;background:gray"></div>'
                            '</div></form>')
        page.wait_for_function("window.submitted === true || testState.status === 'paused'", timeout=15000)
        self.assertTrue(page.evaluate('!!window.submitted'), page.evaluate('testState.message'))
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 1)
        self.assertTrue(page.evaluate("""() => {
            const event=mouseEvents.find(e=>e.tag==='BUTTON' && e.type==='click');
            return document.elementFromPoint(event.x,event.y)===document.querySelector('form button');
        }"""))

    def test_form_becoming_invalid_before_click_releases_reservation(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email" required><button>Continue</button></form>'
                            '<script>const originalSend=chrome.runtime.sendMessage;chrome.runtime.sendMessage=async message=>{'
                            'const result=await originalSend(message);if(message.type==="claim" && message.reserve)'
                            'document.querySelector("input").value="invalid-address";return result;};</script>')
        page.wait_for_function("testState.status==='paused'")
        self.assertFalse(page.evaluate('!!window.submitted'))
        self.assertFalse(page.evaluate('!!testClaims.email'))
        self.assertTrue(page.evaluate("testMessages.some(m=>m.type==='finish-click' && !m.sent)"))
        self.assertFalse(page.evaluate("mouseEvents.some(e=>e.tag==='BUTTON' && e.type==='click')"))

    def test_site_submitting_on_pointerdown_does_not_receive_another_click(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'
                            '<input name="email" type="email"><button onpointerdown="this.form.requestSubmit()">Continue</button></form>')
        page.wait_for_function('window.submits===1')
        count = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+3", arg=count)
        self.assertEqual(page.evaluate('window.submits'), 1)
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='claim' && m.reserve)"))
        self.assertFalse(page.evaluate("mouseEvents.some(e=>e.tag==='BUTTON' && e.type==='click')"))

    def test_button_replaced_while_waiting_is_relocated_before_click(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'
                            '<input name="email" type="email" oninput="if(this.value===\'test@icloud.com\')setTimeout(()=>{'
                            'const old=document.querySelector(\'button\'),fresh=old.cloneNode(true);fresh.disabled=false;'
                            'old.replaceWith(fresh);window.freshButton=fresh},1800)">'
                            '<button id="continue" disabled onclick="window.usedFresh=this===window.freshButton">Continue</button></form>')
        page.wait_for_function('window.submits===1')
        self.assertTrue(page.evaluate('window.usedFresh'))
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 1)

    def test_button_geometry_is_rechecked_after_hover(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email"><button style="width:120px" '
                            'onmouseover="this.style.width=\'280px\'" '
                            'onpointerdown="window.downOffset=Math.abs(event.clientX-(this.getBoundingClientRect().left+this.offsetWidth/2))">Continue</button></form>')
        page.wait_for_function('window.submitted===true')
        self.assertLess(page.evaluate('window.downOffset'), 1)
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 1)

    def test_last_moment_redraw_overlay_or_window_switch_releases_unsent_click(self):
        changes = [
            "const button=document.querySelector('button');button.replaceWith(button.cloneNode(true));",
            "const cover=document.createElement('div');cover.id='cover';cover.style='position:fixed;inset:0;z-index:100';document.body.append(cover);",
            "window.away=true;",
            "testState.active=false;testState.status='paused';",
        ]
        for change in changes:
            with self.subTest(change=change):
                script = '<script>window.away=false;document.hasFocus=()=>!away;const sendOriginal=chrome.runtime.sendMessage;'
                script += 'chrome.runtime.sendMessage=async message=>{const result=await sendOriginal(message);'
                script += 'if(message.type==="claim" && message.reserve && !window.changedOnce){window.changedOnce=true;'+change+'}'
                script += 'if(message.type==="finish-click" && !message.sent){window.cancelled=true;'
                script += 'document.querySelector("#cover")?.remove();window.away=false;testState.active=true;testState.status="running";}return result;};</script>'
                page = self.fixture('<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'
                                    '<input name="email" type="email"><button>Continue</button></form>'+script)
                page.wait_for_function('window.submits===1', timeout=15000)
                self.assertTrue(page.evaluate('!!window.cancelled'))
                self.assertEqual(page.evaluate("testMessages.filter(m=>m.type==='finish-click' && !m.sent).length"), 1)
                self.assertEqual(page.evaluate("testMessages.filter(m=>m.type==='finish-click' && m.sent).length"), 1)
                self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 1)
                page.close()

    def test_async_button_response_waits_for_loading_then_field_change(self):
        page = self.fixture('<form><input name="email" type="email"><button type="button" '
                            'onclick="window.clicks=(window.clicks||0)+1;this.disabled=true;this.form.setAttribute(\'aria-busy\',\'true\')">Continue</button></form>')
        page.wait_for_function("testMessages.some(m=>m.event==='click_response' && m.outcome==='loading')")
        count = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+3", arg=count)
        self.assertEqual(page.evaluate('window.clicks'), 1)
        self.assertFalse(page.evaluate("testMessages.some(m=>m.event==='form_submit')"))
        page.evaluate("testState.mode='manual';document.querySelector('form').outerHTML='<form><input name=code autocomplete=one-time-code></form>'")
        page.wait_for_function("testMessages.some(m=>m.event==='click_response' && m.outcome==='advanced')")
        self.assertEqual(page.evaluate('testState.status'), 'running')

    def test_click_validation_error_pauses_without_repeating_or_exporting_text(self):
        page = self.fixture('<form><input name="email" type="email"><button type="button" '
                            'onclick="window.clicks=(window.clicks||0)+1;this.form.insertAdjacentHTML(\'beforeend\',\'<p role=alert>PRIVATE_VALIDATION_TEXT</p>\')">Continue</button></form>')
        page.wait_for_function("testState.status==='paused'")
        self.assertEqual(page.evaluate('window.clicks'), 1)
        self.assertTrue(page.evaluate("testMessages.some(m=>m.event==='click_response' && m.outcome==='validation')"))
        self.assertNotIn('PRIVATE_VALIDATION_TEXT', page.evaluate('JSON.stringify(testMessages)'))

    def test_click_with_no_response_waits_then_times_out_without_a_second_click(self):
        page = self.fixture('<form><input name="email" type="email"><button type="button" '
                            'onclick="window.clicks=(window.clicks||0)+1">Continue</button></form>')
        page.wait_for_function('window.clicks===1')
        count = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+3", arg=count)
        self.assertEqual(page.evaluate('window.clicks'), 1)
        self.assertEqual(page.evaluate('testState.status'), 'running')
        page.evaluate('window.realNow=Date.now;Date.now=()=>realNow()+46000')
        page.wait_for_function("testState.status==='paused'")
        self.assertTrue(page.evaluate("testMessages.some(m=>m.event==='click_response' && m.outcome==='timeout')"))
        self.assertTrue(page.evaluate("testMessages.some(m=>m.type==='claim' && m.stage==='email' && m.prepare)"))
        self.assertEqual(page.evaluate('window.clicks'), 1)

    def advance_idle_time(self, page, milliseconds=2500):
        page.evaluate('ms=>{window.realNow||=Date.now;window.timeShift=(window.timeShift||0)+ms;Date.now=()=>realNow()+timeShift}', milliseconds)

    def test_continue_submit_without_spinner_does_not_repeat_request(self):
        for stage, fields in [('otp', '<input name="code" autocomplete="one-time-code">'),
                              ('profile', '<input name="name"><input name="age" type="number">')]:
            with self.subTest(stage=stage):
                page = self.fixture('<form onsubmit="event.preventDefault();window.requests=(window.requests||0)+1">'+fields+
                                    '<button onclick="window.clicks=(window.clicks||0)+1">Continue</button></form>')
                page.wait_for_function('window.requests===1')
                page.wait_for_timeout(7500)
                count = page.evaluate("testMessages.filter(m=>m.type==='state').length")
                page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+4", arg=count)
                self.assertEqual(page.evaluate('window.requests'), 1)
                self.assertEqual(page.evaluate('window.clicks'), 1)
                self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='retry-continue')"))
                page.close()

    def test_finish_creating_account_handles_idle_validation_submit(self):
        page = self.fixture('<form onsubmit="event.preventDefault();if(event.submitter)window.finished=true">'
                            '<input name="name"><input name="age" type="number" '
                            'onchange="this.form.requestSubmit()"><button type="button" '
                            'onclick="window.finished=true">Finish creating account</button></form>')
        page.wait_for_function("testMessages.some(m=>m.event==='form_submit')")
        self.advance_idle_time(page, 9000)
        page.wait_for_function('window.finished===true', timeout=12000)
        self.assertEqual(page.evaluate("testMessages.filter(m=>m.type==='finish-click' && m.sent).length"), 1)

    def test_mailbox_reply_after_step_change_does_not_fill_code_into_profile(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.finished=true">'
                            '<input name="code" autocomplete="one-time-code"><button>Continue</button></form>'
                            '<script>const originalSend=chrome.runtime.sendMessage;chrome.runtime.sendMessage=async m=>{'
                            'const reply=await originalSend(m);if(m.type==="code"){'
                            'const input=document.querySelector("input");input.name="name";input.autocomplete="name";'
                            'input.insertAdjacentHTML("afterend","<input name=age type=number>");'
                            'input.oninput=()=>{if(/\\d/.test(input.value))window.staleCode=true};'
                            'history.replaceState({},"","/about-you");}return reply;};</script>')
        page.wait_for_function('window.finished===true', timeout=20000)
        self.assertFalse(page.evaluate('!!window.staleCode'))
        self.assertEqual(page.locator('input[name=name]').input_value(), 'Test User')
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='claim' && m.stage==='otp' && m.reserve)"))

    def test_repurposed_input_stops_old_step_typing_and_submission(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.finished=true">'
                            '<input name="email" type="email" oninput="'
                            'if(this.name===\'email\' && this.value.length===3){'
                            'history.replaceState({},\'\',\'/about-you\');this.type=\'text\';this.name=\'name\';'
                            'this.insertAdjacentHTML(\'afterend\',\'<input name=age type=number>\');}'
                            'else if(this.name===\'name\' && this.value.includes(\'@\'))window.staleWrite=true;'
                            '"><button>Continue</button></form>')
        page.wait_for_function('window.finished===true', timeout=20000)
        self.assertFalse(page.evaluate('!!window.staleWrite'))
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='claim' && m.stage==='email' && m.reserve)"))
        self.assertEqual(page.locator('input[name=name]').input_value(), 'Test User')

    def test_otp_and_profile_continue_retry_twice_without_refilling(self):
        for stage, fields in [('otp', '<input name="code" autocomplete="one-time-code">'),
                              ('profile', '<input name="name"><input name="age" type="number">')]:
            with self.subTest(stage=stage):
                page = self.fixture('<form onsubmit="event.preventDefault();if(window.clicks===3){this.setAttribute(\'aria-busy\',\'true\');window.done=true}">'+fields+
                                    '<button onclick="window.clicks=(window.clicks||0)+1;(window.clickTimes||=[]).push(Date.now());if(clicks<3)event.preventDefault()">Continue</button></form>')
                page.wait_for_function('window.clicks===1')
                typed = page.evaluate('inputEvents.length')
                values = page.locator('input').evaluate_all('nodes=>nodes.map(el=>el.value)')
                for clicks in [2, 3]:
                    # Use real elapsed time to catch an unchanged 8-second retry threshold.
                    page.wait_for_function('n=>window.clicks===n', arg=clicks, timeout=7000)
                times = page.evaluate('window.clickTimes')
                self.assertTrue(all(2000 <= later-earlier < 6500 for earlier, later in zip(times, times[1:])), times)
                self.assertTrue(page.evaluate('window.done'))
                self.assertEqual(page.evaluate('inputEvents.length'), typed)
                self.assertEqual(page.locator('input').evaluate_all('nodes=>nodes.map(el=>el.value)'), values)
                self.assertEqual(page.evaluate("testMessages.filter(m=>m.type==='code').length"), int(stage=='otp'))
                page.close()

    def test_review_false_submit_unlocks_then_only_retries_after_real_continue(self):
        for fields in ['<input name="code" autocomplete="one-time-code" onchange="this.form.requestSubmit()">',
                       '<input name="name"><input name="age" type="number" onchange="this.form.requestSubmit()">']:
            with self.subTest(fields=fields):
                page = self.fixture('<script>testState.mode="review"</script><form onsubmit="event.preventDefault()">'+fields+
                                    '<button type="button" onclick="window.clicks=(window.clicks||0)+1;if(clicks===2)this.form.setAttribute(\'aria-busy\',\'true\')">Continue</button></form>')
                page.wait_for_function("testMessages.some(m=>m.event==='form_submit')")
                self.advance_idle_time(page, 9000)
                page.wait_for_function("testMessages.some(m=>m.type==='review-ready')")
                self.assertFalse(page.evaluate('!!window.clicks'))
                self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='retry-continue')"))
                values = page.locator('input').evaluate_all('nodes=>nodes.map(el=>el.value)')
                page.locator('form button').click()
                page.wait_for_function("testMessages.some(m=>m.type==='continue-observed')")
                self.advance_idle_time(page)
                page.wait_for_function('window.clicks===2')
                self.assertEqual(page.locator('input').evaluate_all('nodes=>nodes.map(el=>el.value)'), values)
                page.close()

    def test_continue_retry_is_bounded_when_page_never_responds(self):
        page = self.fixture('<form onsubmit="event.preventDefault()"><input name="code" autocomplete="one-time-code">'
                            '<button type="button" onclick="window.clicks=(window.clicks||0)+1">Continue</button></form>')
        page.wait_for_function('window.clicks===1')
        for clicks in [2, 3]:
            self.advance_idle_time(page)
            page.wait_for_function('n=>window.clicks===n', arg=clicks)
        self.advance_idle_time(page)
        page.wait_for_function("testState.status==='paused'")
        self.assertEqual(page.evaluate('window.clicks'), 3)
        self.assertIn('补点次数', page.evaluate('testState.message'))

    def test_transient_loading_prevents_continue_retry_after_becoming_idle(self):
        for effect in ["this.disabled=true;this.disabled=false;",
                       "this.form.setAttribute('aria-busy','true');this.form.removeAttribute('aria-busy');",
                       "const spinner=document.createElement('i');spinner.className='animate-spin';this.form.append(spinner);spinner.remove();"]:
            with self.subTest(effect=effect):
                page = self.fixture('<form onsubmit="event.preventDefault()"><input name="code" autocomplete="one-time-code">'
                                    '<button type="button" onclick="window.clicks=(window.clicks||0)+1;'+effect+'">Continue</button></form>')
                page.wait_for_function('window.clicks===1')
                self.advance_idle_time(page)
                page.wait_for_function("testMessages.some(m=>m.event==='click_response' && m.outcome==='loading')")
                count = page.evaluate("testMessages.filter(m=>m.type==='state').length")
                page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+2", arg=count)
                self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='retry-continue')"))
                self.assertEqual(page.evaluate('window.clicks'), 1)
                page.close()

    def test_otp_submit_during_pointerdown_stays_guarded_after_idle_interval(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'
                            '<input name="code" autocomplete="one-time-code"><button onpointerdown="this.form.requestSubmit()">Continue</button></form>')
        page.wait_for_function('window.submits===1')
        self.advance_idle_time(page)
        count = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+3", arg=count)
        self.assertEqual(page.evaluate('window.submits'), 1)
        self.assertFalse(page.evaluate("mouseEvents.some(e=>e.tag==='BUTTON' && e.type==='click')"))

    def test_new_submit_during_retry_pointerdown_stops_further_clicks(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'
                            '<input name="code" autocomplete="one-time-code">'
                            '<button type="button" onpointerdown="if(window.clicks)this.form.requestSubmit()" onclick="window.clicks=(window.clicks||0)+1">Continue</button></form>')
        page.wait_for_function('window.clicks===1')
        self.advance_idle_time(page)
        page.wait_for_function('window.submits===1')
        count = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+3", arg=count)
        self.assertEqual(page.evaluate('window.submits'), 1)
        self.assertEqual(page.evaluate('window.clicks'), 1)
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='retry-continue')"))

    def test_continue_retry_rechecks_loading_after_worker_reservation(self):
        page = self.fixture('<form onsubmit="event.preventDefault()"><input name="code" autocomplete="one-time-code">'
                            '<button type="button" onclick="window.clicks=(window.clicks||0)+1">Continue</button></form>'
                            '<script>const originalSend=chrome.runtime.sendMessage;chrome.runtime.sendMessage=async message=>{'
                            'const result=await originalSend(message);if(message.type==="retry-continue")'
                            'document.querySelector("form").setAttribute("aria-busy","true");return result;};</script>')
        page.wait_for_function('window.clicks===1')
        self.advance_idle_time(page)
        page.wait_for_function("testMessages.some(m=>m.type==='finish-click' && !m.sent)")
        self.assertEqual(page.evaluate('window.clicks'), 1)
        self.assertEqual(page.evaluate('testContinueRetries.otp'), 0)
        self.assertTrue(page.evaluate('testClaims.otp'))

    def test_continue_retry_waits_for_foreground_and_stops_on_field_change(self):
        page = self.fixture('<script>window.away=false;document.hasFocus=()=>!away;</script>'
                            '<form onsubmit="event.preventDefault()"><input name="code" autocomplete="one-time-code">'
                            '<button type="button" onclick="window.clicks=(window.clicks||0)+1">Continue</button></form>')
        page.wait_for_function('window.clicks===1')
        page.evaluate('window.away=true')
        self.advance_idle_time(page)
        count = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("n=>testMessages.filter(m=>m.type==='state').length>=n+3", arg=count)
        self.assertFalse(page.evaluate("testMessages.some(m=>m.type==='retry-continue')"))
        page.evaluate('window.away=false;document.querySelector("input").value="000000"')
        page.wait_for_function("testState.status==='paused'")
        self.assertEqual(page.evaluate('window.clicks'), 1)

    def test_mouse_sequence_waits_for_enabled_button_and_clicks_once(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submits=(window.submits||0)+1">'
                            '<input name="email" type="email" oninput="if(this.value===\'test@icloud.com\')setTimeout(()=>document.querySelector(\'button\').disabled=false,1800)">'
                            '<button disabled>Continue</button></form>')
        page.wait_for_function('window.submits === 1')
        for tag in ['INPUT', 'BUTTON']:
            events = page.evaluate('(tag)=>mouseEvents.filter(e=>e.tag===tag)', tag)
            self.assertEqual([e['type'] for e in events], ['pointerover','mouseover','pointerenter','mouseenter',
                             'pointermove','mousemove','pointerdown','mousedown','pointerup','mouseup','click'])
            self.assertTrue(all(e['buttons'] == (1 if e['type'] in ['pointerdown','mousedown'] else 0) for e in events))
            self.assertTrue(all(e['x'] > 0 and e['y'] > 0 for e in events))
        calls = page.evaluate("testMessages.filter(m=>m.type==='state').length")
        page.wait_for_function("count=>testMessages.filter(m=>m.type==='state').length >= count+2", arg=calls)
        self.assertEqual(page.evaluate('submits'), 1)

    def test_mouse_does_not_click_through_overlay_or_after_pause(self):
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email"><button>Continue</button></form>'
                            '<div style="position:fixed;inset:0;z-index:100;background:white"></div>')
        page.wait_for_function("testState.status === 'paused'")
        self.assertFalse(page.evaluate('!!window.submitted'))
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.type==='click').length"), 0)
        self.assertFalse(page.evaluate('!!testClaims.email'))
        page.close()
        page = self.fixture('<form onsubmit="event.preventDefault();window.submitted=true">'
                            '<input name="email" type="email"><button onpointerdown="testState.active=false;testState.status=\'paused\'">Continue</button></form>')
        page.wait_for_function("mouseEvents.some(e=>e.tag==='BUTTON' && e.type==='mouseup')")
        self.assertFalse(page.evaluate('!!window.submitted'))
        self.assertFalse(page.evaluate('!!testClaims.email'))
        self.assertEqual(page.evaluate("mouseEvents.filter(e=>e.tag==='BUTTON' && e.type==='click').length"), 0)
        page.evaluate("document.querySelector('button').onpointerdown=null;testState.active=true;testState.status='running'")
        page.wait_for_function('window.submitted === true')

    def test_finished_job_stops_polling_and_does_not_touch_later_page(self):
        page = self.fixture('<div id="prompt-textarea" contenteditable="true"></div>', 'https://chatgpt.com/')
        page.wait_for_function("testState.status==='done'")
        page.wait_for_timeout(2000)  # Let the loop observe the completed worker state.
        count = page.evaluate('testMessages.length')
        page.evaluate('document.body.innerHTML=\'<form><input name="email" type="email"><button>Continue</button></form>\'')
        page.wait_for_timeout(3500)
        self.assertEqual(page.evaluate('testMessages.length'), count)
        self.assertEqual(page.locator('input').input_value(), '')

    def test_phone_and_captcha_pause_and_completion_checks_email(self):
        for html, reason in [('<input type="tel" name="phone">', '手机验证'), ('<h1>Verify you are human</h1>', '人机验证')]:
            page = self.fixture(html)
            page.wait_for_function("testState.status === 'paused'")
            self.assertIn(reason, page.evaluate("testState.message"))
            self.assertFalse(page.evaluate("testMessages.some(message=>message.type==='code')"))
            page.close()
        page = self.fixture('<div id="prompt-textarea" contenteditable="true"></div>', "https://chatgpt.com/")
        page.wait_for_function("testState.status === 'done'")
        self.assertEqual(page.evaluate("testMessages.find(m=>m.type==='complete').email"), "test@icloud.com")
        self.assertEqual(page.evaluate("testMessages.filter(m=>m.event==='session_check').length"), 2)

    def test_email_only_in_incognito_with_direct_mailbox_requests(self):
        with tempfile.TemporaryDirectory(prefix="team48-extension-test-") as tmp, mailbox_server() as (origin, requests):
            test_extension = Path(tmp) / "extension"
            # Never copy the user's real embedded credential to the test installation.
            shutil.copytree(EXTENSION, test_extension, ignore=shutil.ignore_patterns("private-config.mjs"))
            (test_extension / "private-config.mjs").write_text("export const MAILBOX = " + json.dumps(
                {"baseUrl": origin, "address": "inbox@example.com", "adminPassword": "fixture-secret"}) + ";", encoding="utf-8")
            mail_source = (test_extension / "cloudflare.mjs").read_text(encoding="utf-8")
            mail_source = mail_source.replace("https://apimail.xiaozhudf2026.foo", origin)
            (test_extension / "cloudflare.mjs").write_text(mail_source, encoding="utf-8")
            manifest = json.loads((test_extension / "manifest.json").read_text(encoding="utf-8"))
            manifest["host_permissions"] = [origin + "/*"]
            (test_extension / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            context = self.playwright.chromium.launch_persistent_context(
                str(Path(tmp) / "profile"), channel="chromium", headless=True,
                args=[f"--disable-extensions-except={test_extension}", f"--load-extension={test_extension}"],
                viewport={"width": 368, "height": 780})
            try:
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
                extension_id = worker.url.split("/")[2]
                page = context.new_page()
                errors = []
                context.on("page", lambda opened: opened.on("pageerror", lambda error: errors.append(str(error))))
                page.goto(f"chrome-extension://{extension_id}/popup.html")
                expect(page.locator('#window-badge')).to_have_text('普通窗口')
                expect(page.locator('#start')).to_be_disabled()
                self.assertEqual(page.locator('#settings,#token,#console-link').count(), 0)
                page.goto("chrome://extensions/")
                page.evaluate("() => chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode:true})")
                page.evaluate("id => chrome.developerPrivate.updateExtensionConfiguration({extensionId:id,incognitoAccess:true})", extension_id)
                page.wait_for_function("""async id => (await chrome.developerPrivate.getExtensionsInfo({includeDisabled:true}))
                    .some(x=>x.id===id && x.state==='ENABLED' && x.incognitoAccess.isActive)""", arg=extension_id)
                page.goto(f"chrome-extension://{extension_id}/popup.html")
                with context.expect_page() as opened:
                    page.evaluate("() => chrome.windows.create({incognito:true,url:'about:blank'})")
                private = opened.value
                private.goto(f'chrome-extension://{extension_id}/popup.html')
                self.assertTrue(private.evaluate('chrome.extension.inIncognitoContext'))
                expect(private.locator('#start')).to_be_enabled()
                self.assertEqual(private.locator('input:not([readonly]):not([type=checkbox]):visible').count(), 1)
                self.assertEqual(len(requests), 0)
                private.locator('#hide-panel').check()
                private.reload()
                expect(private.locator('#hide-panel')).to_be_checked()
                private.locator('#workflow').select_option('submit')
                expect(private.locator('#hide-panel')).to_be_disabled()
                private.locator('#workflow').select_option('auto')
                expect(private.locator('#hide-panel')).to_be_enabled()
                private.locator('#hide-panel').uncheck()
                private.locator('body').screenshot(path=str(OUTPUT / 'signup-extension-idle.png'))
                context.route('https://chatgpt.com/**', lambda route: route.fulfill(content_type='text/html', body=
                    '<form onsubmit="event.preventDefault();window.submitted=true">'
                    '<input name="code" autocomplete="one-time-code"><button>Continue</button></form>'
                    '<script>window.typed=[];document.addEventListener("input",e=>typed.push({value:e.target.value,data:e.data,inputType:e.inputType}))</script>'))
                private.locator('#email').fill('test@icloud.com')
                with context.expect_page() as opened:
                    private.locator('#start').click()
                registration = opened.value
                registration.wait_for_url('https://chatgpt.com/')
                registration.wait_for_function('window.submitted === true', timeout=20000)
                self.assertEqual(registration.locator('input[name=code]').input_value(), '005239')
                self.assertEqual(registration.evaluate('typed.map(e=>e.value)'), ['0', '00', '005', '0052', '00523', '005239'])
                self.assertTrue(registration.evaluate("typed.every(e=>e.inputType==='insertText' && e.data.length===1)"))
                self.assertGreaterEqual(len(requests), 2)
                self.assertTrue(all(item['valid'] for item in requests))
                self.assertTrue(all(item['path'] == '/admin/mails' for item in requests))
                expect(private.locator('#progress')).to_be_visible()
                self.assertEqual(private.locator('#settings,#token,#console-link').count(), 0)
                private.locator('body').screenshot(path=str(OUTPUT / 'signup-extension-progress.png'))
                private.evaluate("() => chrome.runtime.sendMessage({type:'stop'})")
                self.assertFalse(errors, errors)
            finally:
                context.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
