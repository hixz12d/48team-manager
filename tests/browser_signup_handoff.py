"""Real unpacked extension: signup, then Team48 handoff and OAuth in the same incognito tab.

Team48 is a local stub and every OpenAI page is a local fixture; no real account is used.
"""
import json
import shutil
import tempfile
import threading
import unittest
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.browser_signup_extension import EXTENSION, ROOT, mailbox_server
from tests.browser_signup_flow import wait_job

TOKEN = "fixture-team48-token-0123456789"
STATE = "fixture-state"

OAUTH_HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><style>
body{margin:0;background:#202020;color:#fafafa;font:18px system-ui}main{width:min(420px,90vw);margin:80px auto}
input{width:100%;height:56px;border-radius:28px;border:1px solid #777;background:transparent;color:inherit;font:inherit;padding:16px}
button{width:100%;height:52px;border-radius:26px;border:0;margin:16px 0;font:inherit;cursor:pointer}
.team{display:flex;gap:10px;align-items:center;padding:12px;border:1px solid #555;border-radius:12px;margin:8px 0}
</style></head><body><main id="app"></main><script>
const app=document.querySelector('#app'), path=location.pathname;
const save=(key,value)=>sessionStorage.setItem(key,value);
const log=entry=>localStorage.setItem('oauth',JSON.stringify([...JSON.parse(localStorage.getItem('oauth')||'[]'),entry]));
const form=(title,fields)=>{app.innerHTML=`<h1>${title}</h1><form>${fields}<button type="submit">Continue</button></form>`;return app.querySelector('form')};
if(location.search.includes('inspect=1')) {}
else if(path==='/oauth/authorize') { log('authorize:'+new URLSearchParams(location.search).get('state')); location.replace('/log-in'); }
else if(path==='/log-in') {
 const f=form('Welcome back','<input type="email" name="email" autocomplete="email" placeholder="Email address" required>');
 f.onsubmit=e=>{e.preventDefault();log('email:'+f.email.value);setTimeout(()=>location.assign('/log-in/password'),150)};
} else if(path==='/log-in/password') {
 const f=form('Enter your password','<input type="password" name="password" autocomplete="current-password" required>');
 f.onsubmit=e=>{e.preventDefault();log('password:'+f.password.value.length);setTimeout(()=>location.assign('/workspace'),150)};
} else if(path==='/workspace') {
 app.innerHTML=`<h1>Choose a workspace</h1><form>
  <label class="team"><input type="radio" name="ws" value="personal">Personal account</label>
  <label class="team"><input type="radio" name="ws" value="other">Other Team</label>
  <label class="team"><input type="radio" name="ws" value="alpha">Alpha</label>
  <button type="submit">Continue</button></form>`;
 const f=app.querySelector('form');
 f.onsubmit=e=>{e.preventDefault();log('workspace:'+(f.ws.value||'none'));setTimeout(()=>location.assign('/consent'),150)};
} else if(path==='/consent') {
 app.innerHTML='<h1>Codex CLI wants to access your OpenAI account</h1><button type="button">Continue</button>';
 app.querySelector('button').onclick=()=>{log('consent');
  location.assign('http://localhost:1455/auth/callback?code=fixture-code&state=%STATE%')};
}
</script></body></html>""".replace("%STATE%", STATE)


@contextmanager
def team48_server():
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def reply(self, payload, status=200):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            return self.headers.get("Authorization") == f"Bearer {TOKEN}" and not self.headers.get("Cookie")

        def do_GET(self):
            calls.append({"path": self.path, "authorized": self.authorized()})
            if self.path == "/api/ext/workspaces" and self.authorized():
                return self.reply({"ok": True, "items": [{"id": 3, "name": "Alpha"}, {"id": 4, "name": "Other Team"}]})
            self.reply({"detail": "denied"}, 401)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            calls.append({"path": self.path, "authorized": self.authorized(), "body": body})
            if not self.authorized():
                return self.reply({"detail": "denied"}, 401)
            if self.path == "/api/ext/handoff" and not body.get("sync_operation_id"):
                return self.reply({"ok": True, "state": "syncing", "operation_id": "sync-1"})
            if self.path == "/api/ext/handoff":
                return self.reply({"ok": True, "state": "authorize", "account_id": 11, "ticket": "fixture-ticket",
                                   "authorize_url": f"https://auth.openai.com/oauth/authorize?state={STATE}&login_hint=test%40icloud.com",
                                   "workspace": {"id": 3, "name": "Alpha", "names": ["Alpha"]}})
            if self.path == "/api/ext/handoff/complete":
                return self.reply({"ok": True, "message": "test@icloud.com 授权已更新", "followups": {
                    "sub2api": {"ok": True, "message": "Sub2API 推送完成"},
                    "switch_count": {"ok": True, "counted": True, "message": "今日切换 +1，现为 1 次"}}})
            self.reply({"detail": "not found"}, 404)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class SignupHandoffTests(unittest.TestCase):
    def test_signup_then_automatic_team48_authorization_in_the_same_tab(self):
        deliver = threading.Event()
        deliver.set()
        with sync_playwright() as p, tempfile.TemporaryDirectory(prefix="team48-handoff-") as tmp, \
                mailbox_server(deliver) as (mail_origin, mail_requests), team48_server() as (team48_origin, calls):
            extension = Path(tmp) / "extension"
            shutil.copytree(EXTENSION, extension, ignore=shutil.ignore_patterns("private-config.mjs"))
            (extension / "private-config.mjs").write_text(
                "export const MAILBOX = " + json.dumps({"baseUrl": mail_origin, "address": "inbox@example.com", "adminPassword": "fixture-secret"}) + ";\n"
                "export const TEAM48 = " + json.dumps({"baseUrl": team48_origin, "token": TOKEN}) + ";\n", encoding="utf-8")
            mail = extension / "cloudflare.mjs"
            mail.write_text(mail.read_text(encoding="utf-8").replace("https://apimail.xiaozhudf2026.foo", mail_origin), encoding="utf-8")
            manifest = json.loads((extension / "manifest.json").read_text(encoding="utf-8"))
            manifest["host_permissions"] = [mail_origin + "/*", team48_origin + "/*"]
            (extension / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            context = p.chromium.launch_persistent_context(str(Path(tmp) / "profile"), channel="chromium", headless=True,
                args=[f"--disable-extensions-except={extension}", f"--load-extension={extension}"],
                viewport={"width": 980, "height": 800})
            try:
                errors = []
                context.on("page", lambda page: page.on("pageerror", lambda error, page=page: errors.append(page.url + " " + str(error))))
                worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
                extension_id = worker.url.split("/")[2]
                admin = context.new_page()
                admin.goto("chrome://extensions/")
                admin.evaluate("() => chrome.developerPrivate.updateProfileConfiguration({inDeveloperMode:true})")
                admin.evaluate("id => chrome.developerPrivate.updateExtensionConfiguration({extensionId:id,incognitoAccess:true})", extension_id)
                admin.wait_for_function("""async id => (await chrome.developerPrivate.getExtensionsInfo({includeDisabled:true}))
                    .some(x=>x.id===id && x.incognitoAccess.isActive)""", arg=extension_id)
                admin.goto(f"chrome-extension://{extension_id}/popup.html")
                with context.expect_page() as created:
                    admin.evaluate("chrome.windows.create({incognito:true,url:'about:blank'})")
                popup = created.value
                popup.goto(f"chrome-extension://{extension_id}/popup.html")
                signup_html = (ROOT / "tests/fixtures/signup_flow.html").read_text(encoding="utf-8")
                oauth_paths = ("/oauth/authorize", "/log-in", "/workspace", "/consent")

                def route(request):
                    url = request.request.url
                    if url.endswith("/api/auth/session"):
                        request.fulfill(json={"user": {"email": "test@icloud.com"}})
                    elif url.startswith("https://auth.openai.com") and any(path in url for path in oauth_paths):
                        request.fulfill(content_type="text/html", body=OAUTH_HTML)
                    else:
                        request.fulfill(content_type="text/html", body=signup_html)
                context.route("https://chatgpt.com/**", route)
                context.route("https://auth.openai.com/**", route)

                expect(popup.locator("#team48-auto")).to_be_visible()
                expect(popup.locator("#auto-workspace option")).to_have_count(3)
                popup.locator("#auto-workspace").select_option("3")
                popup.locator("#email").fill("test@icloud.com")
                with context.expect_page() as opened:
                    popup.locator("#start").click()
                registration = opened.value
                registration.bring_to_front()
                try:
                    wait_job(popup, ["handoff", "status"], "authorizing", timeout=90000)
                    wait_job(popup, ["handoff", "status"], "done", timeout=90000)
                except Exception as error:
                    job = popup.evaluate("async () => (await chrome.storage.session.get('job')).job")
                    raise AssertionError(json.dumps({"url": registration.url, "status": job.get("status"), "message": job.get("message"),
                                                     "phase": job.get("phase"), "handoff": job.get("handoff"),
                                                     "calls": [call["path"] for call in calls]}, ensure_ascii=False)) from error
                posted = [call for call in calls if call["path"].startswith("/api/ext/handoff")]
                self.assertEqual([call["path"] for call in posted], ["/api/ext/handoff", "/api/ext/handoff", "/api/ext/handoff/complete"])
                self.assertTrue(all(call["authorized"] for call in calls))
                self.assertEqual(posted[0]["body"], {"email": "test@icloud.com", "workspace_id": 3})
                complete = posted[2]["body"]
                self.assertEqual(complete["ticket"], "fixture-ticket")
                self.assertEqual(complete["callback_url"], f"http://localhost:1455/auth/callback?code=fixture-code&state={STATE}")
                self.assertTrue(complete["push_sub2api"] and complete["count_switch"])
                job = popup.evaluate("async () => (await chrome.storage.session.get('job')).job")
                # The login used the password generated at signup; the right team was chosen.
                registration.wait_for_url(f"chrome-extension://{extension_id}/popup.html")
                expect(registration.locator("#handoff-steps li")).to_have_count(3)
                expect(registration.locator("#handoff-steps")).to_contain_text("今日切换 +1")
                expect(popup.locator("#handoff-title")).to_have_text("已接入 Team48")
                self.assertEqual(job["phase"], "oauth")
                self.assertEqual(job["attempts"].get("consent"), 2)
                self.assertNotIn("fixture-ticket", json.dumps(job))
                # Read the fixture's log in the same (routed) incognito tab; the finished job ignores it.
                registration.goto("https://auth.openai.com/log-in?inspect=1")
                log = json.loads(registration.evaluate("localStorage.getItem('oauth')") or "[]")
                self.assertEqual(log[0], f"authorize:{STATE}")
                self.assertIn("email:test@icloud.com", log)
                self.assertIn("password:" + str(len(job["password"])), log)
                self.assertIn("workspace:alpha", log)
                self.assertEqual(log[-1], "consent")
                self.assertFalse(errors, errors)
                self.assertTrue(all(item["valid"] for item in mail_requests))
            finally:
                context.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
