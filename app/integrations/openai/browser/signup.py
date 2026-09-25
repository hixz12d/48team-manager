"""Run the extension's signup UI in a project-owned Chromium/Chromix context.

No extension installation, website-world bindings, HTTP control endpoint or
mailbox credentials in JavaScript. The caller owns the browser and OAuth handoff.
"""
from __future__ import annotations

from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date
import json
from pathlib import Path
import secrets
import time
import uuid
from urllib.parse import urlsplit

from app.integrations.openai.browser.signup_state import HOSTS, SignupState, trusted_url
from app.integrations.openai.browser.signup_readiness import SignupReadiness, HOME_READY_TIMEOUT

ASSET_DIR = Path(__file__).resolve().parents[4] / "extensions" / "chatgpt-signup"


def signup_assets():
    content = (ASSET_DIR / "content.js").read_text(encoding="utf-8")
    version = json.loads((ASSET_DIR / "manifest.json").read_text(encoding="utf-8"))["version"]
    if f"const VERSION = '{version}';" not in content or "managed?.onStop" not in content:
        raise ValueError("Registration asset version mismatch")
    return content, version


def validate_signup_assets():
    from app.integrations.openai.browser.environment import BrowserEnvironmentError

    try:
        signup_assets()
    except (OSError, ValueError, KeyError):
        raise BrowserEnvironmentError("新版注册脚本缺失或版本不一致，请检查部署资源") from None


def signup_profile(profile_dir):
    """Keep one name/birthday across retries, alongside this account's profile."""
    path = Path(profile_dir) / ".team48-signup-profile.json"
    if not path.exists():
        today = date.today()
        age = 22 + secrets.randbelow(24)
        month, day = 1 + secrets.randbelow(12), 1 + secrets.randbelow(28)
        year = today.year - age - ((month, day) > (today.month, today.day))
        value = {"name": secrets.choice(["James", "Oliver", "Henry", "Daniel", "Emma", "Amelia", "Grace", "Sophie"]) + " " +
                 secrets.choice(["Smith", "Taylor", "Wilson", "Clark", "Walker", "Green", "Carter", "Bennett"]),
                 "birthday": date(year, month, day).isoformat()}
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("x", encoding="utf-8") as output:
                json.dump(value, output)
        except FileExistsError:
            pass
    value = json.loads(path.read_text(encoding="utf-8"))
    birthday = date.fromisoformat(value["birthday"])
    if not isinstance(value.get("name"), str) or not 1 <= len(value["name"]) <= 80 or birthday > date.today():
        raise ValueError("Invalid saved signup profile")
    return {"name": value["name"], "birthday": birthday.isoformat()}


class MailboxPoller:
    """A slow mailbox request must not block pause, navigation or state checks."""
    def __init__(self, read_codes):
        self.read_codes = read_codes
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="signup-mail")
        self.pending = None

    def __call__(self):
        if self.pending is None:
            self.pending = self.pool.submit(self.read_codes)
        if not self.pending.done():
            return []
        pending, self.pending = self.pending, None
        return pending.result()

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)


class SignupBridge:
    def __init__(self, page, state, content):
        self.page, self.state = page, state
        self.world = "team48-signup-" + uuid.uuid4().hex
        self.binding = "team48Signup" + uuid.uuid4().hex
        self.contexts = {}
        self.queue = deque()
        self.closed = False
        self.script_id = None
        self.cdp = page.context.new_cdp_session(page)
        self.cdp.on("Runtime.executionContextCreated", self._created)
        self.cdp.on("Runtime.executionContextDestroyed", self._destroyed)
        self.cdp.on("Runtime.executionContextsCleared", lambda _: self.contexts.clear())
        self.cdp.on("Runtime.bindingCalled", self._called)
        self.cdp.send("Page.enable")
        self.frame = self.cdp.send("Page.getFrameTree")["frameTree"]["frame"]["id"]
        self.cdp.send("Runtime.enable")
        self.cdp.send("Runtime.addBinding", {"name": self.binding, "executionContextName": self.world})
        bootstrap = Path(__file__).with_name("signup_bridge.js").read_text(encoding="utf-8")
        config = json.dumps({"binding": self.binding, "hosts": sorted(HOSTS)})
        source = "(() => { if(window!==window.top || location.protocol!=='https:' || !" + json.dumps(sorted(HOSTS)) + ".includes(location.host))return;\n"
        source += "(" + bootstrap + ")(" + config + ");\n"
        source += "const start=()=>{if(!globalThis.__team48ManagedSignup)return;\n" + content + "\n};\n"
        source += "if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',start,{once:true});else start();\n})();"
        self.script_id = self.cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": source, "worldName": self.world})["identifier"]

    def _created(self, event):
        context = event["context"]
        if context.get("name") == self.world and context.get("auxData", {}).get("frameId") == self.frame:
            self.contexts[context["id"]] = context.get("origin")

    def _destroyed(self, event):
        self.contexts.pop(event["executionContextId"], None)

    def _called(self, event):
        if not self.closed and event.get("name") == self.binding and len(self.queue) < 256:
            self.queue.append(event)

    def pump(self):
        # Never run nested Playwright actions inside protocol event callbacks.
        for _ in range(min(len(self.queue), 64)):
            event = self.queue.popleft()
            context_id = event["executionContextId"]
            origin = self.contexts.get(context_id)
            if origin is None or len(event.get("payload", "")) > 8192:
                continue
            try:
                request = json.loads(event["payload"])
                url, request_id = request["url"], request["id"]
                parsed = urlsplit(url)
                if type(request_id) is not int or not trusted_url(url) or origin != f"https://{parsed.netloc}" or url != self.page.url:
                    continue
                response = self.state.handle(request["message"], url)
                # The mailbox operation can overlap navigation; do not answer a stale world.
                if context_id not in self.contexts or self.page.url != url:
                    continue
                self.cdp.send("Runtime.evaluate", {"contextId": context_id,
                    "expression": f"globalThis.__team48ManagedSignupReply({request_id},{json.dumps(response)})"})
            except (ValueError, KeyError, TypeError):
                self.state.fail("registration_bridge_invalid", "注册桥接消息无效，已停止")
            except Exception:
                # Expected when a click navigates before its acknowledgement arrives.
                if context_id in self.contexts and self.page.url == request.get("url"):
                    self.state.fail("registration_bridge_failed", "注册桥接中断，请继续同一邮箱处理")

    def readiness(self):
        if self.closed or urlsplit(self.page.url).netloc != "chatgpt.com":
            return {}
        for context_id, origin in reversed(list(self.contexts.items())):
            if origin != "https://chatgpt.com":
                continue
            try:
                response = self.cdp.send("Runtime.evaluate", {"contextId": context_id,
                    "expression": "globalThis.__team48SignupReadiness?.()", "returnByValue": True})
                value = response.get("result", {}).get("value")
                if isinstance(value, dict) and value.get("url") == self.page.url:
                    return {**value, "document": context_id}
            except Exception:
                pass  # Navigation destroys old worlds; an absent observation is never ready.
        return {}

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.state.status = "stopped"
        try:
            if self.script_id:
                self.cdp.send("Page.removeScriptToEvaluateOnNewDocument", {"identifier": self.script_id})
            for context_id in list(self.contexts):
                try:
                    stopped = self.cdp.send("Runtime.evaluate", {"contextId": context_id,
                        "expression": "globalThis.__team48ManagedSignupStop?.()", "awaitPromise": True, "timeout": 10000})
                    if stopped.get("exceptionDetails"):
                        raise RuntimeError("Registration stop rejected")
                except Exception:
                    if context_id in self.contexts and not self.page.is_closed():
                        raise RuntimeError("Registration runner did not stop") from None
            self.cdp.send("Runtime.removeBinding", {"name": self.binding})
        finally:
            self.queue.clear()
            self.cdp.detach()


def run_managed_signup(*, browser, page, email, password, profile_dir, start_url,
                       invite_entry, team_name, mail_kwargs, report):
    from app.integrations.mail.otp import list_mailbox_codes
    from app.integrations.openai.browser import onboard

    def read_codes():
        return list_mailbox_codes(**mail_kwargs)

    try:
        content, version = signup_assets()
        profile = signup_profile(profile_dir)
        baseline = read_codes()
    except Exception:
        return {"ok": False, "error_code": "registration_preflight_failed",
                "error": "新版注册资源、资料档案或邮箱快照不可用，未开始注册"}
    poller = MailboxPoller(read_codes)
    state = SignupState(email=email, password=password, profile=profile, version=version,
                        baseline=baseline, read_codes=poller, invite_entry=invite_entry, report=report)
    gate = SignupReadiness()
    checkpoints = []
    seen = set()

    def checkpoint(stage, message, *, repeat=False):
        if stage not in seen or repeat:
            seen.add(stage)
            checkpoints.append({"stage": stage, "ms": max(0, int((time.monotonic() - state.started) * 1000))})
            report(stage, message)

    bridge = None
    session = {}
    actions = 0
    try:
        bridge = SignupBridge(page, state, content)
        page.bring_to_front()
        report("signup_runner", "使用新版注册流程（共享插件填写逻辑）")
        onboard.goto_with_retries(page, start_url, report=report)
        last_boundary, boundary_since = "", time.monotonic()
        authenticated_since = None
        reopened = False
        while state.status == "running" and time.monotonic() - state.started < 600:
            page.wait_for_timeout(100)
            bridge.pump()
            if state.status != "running":
                break
            # DOM revisions also retain short loading/modal changes between polls.
            snapshot = bridge.readiness()
            gate.observe(snapshot)
            if urlsplit(page.url).netloc != "chatgpt.com" or snapshot.get("forms"):
                authenticated_since = None  # Returning to registration retains its original budget.
            if authenticated_since is not None and time.monotonic() - authenticated_since >= HOME_READY_TIMEOUT:
                state.fail("registration_home_not_ready", "已登录，但主页或工作空间页面未稳定完成；保留账号，未启动授权")
                break
            url, state.boundary_url = state.boundary_url, None
            if not url or url != page.url:
                continue
            if url != last_boundary:
                last_boundary, boundary_since = url, time.monotonic()
            # Check identity before any workspace action, even on unknown pages.
            before = gate.key
            peeked = onboard._peek_session(page)
            after = bridge.readiness()
            gate.observe(after)
            if page.url != url or not after:
                continue
            actual = ""
            if onboard.session_access_token(peeked):
                user = onboard.session_user(peeked)
                actual = str(user.get("email") or "").strip().lower()
                if actual != state.email:
                    state.fail("token_identity_mismatch", "登录态邮箱与任务不一致")
                    break
                if user.get("emailVerified") is False or user.get("email_verified") is False:
                    state.fail("email_unverified", "登录态报告邮箱尚未验证")
                    break
            if not actual:
                gate.reset()
                idle = time.monotonic() - boundary_since
                if not invite_entry and not state.entry_seen and not state.entry_fallback and url == "https://chatgpt.com/" and idle >= 15:
                    state.entry_fallback = True
                    onboard.goto_with_retries(page, "https://chatgpt.com/auth/login", report=report)
                elif idle > 30:
                    state.fail("registration_page_unrecognized", "未能确认登录或邀请页面，请继续同一邮箱处理")
                continue
            if authenticated_since is None:
                authenticated_since = time.monotonic()
            checkpoint("signup_wait_home", "已识别目标邮箱，等待主页与工作空间页面完成")
            if not after["foreground"] or after["forms"] or after["busy"]:
                continue
            if onboard._accept_terms(page) or ((invite_entry or team_name) and onboard._pick_workspace(page, team_name)):
                gate.reset()
                actions += 1
                checkpoint("signup_workspace", "已操作工作空间或条款确认，重新检查页面就绪", repeat=True)
                if actions > 6:
                    state.fail("invitation_page_stuck", "邀请确认页面未前进，请继续同一邮箱处理")
                continue
            if invite_entry and not reopened:
                reopened = True
                gate.reset()
                onboard.goto_with_retries(page, start_url, report=report)
                continue
            # Recheck after all DOM actions and the network session read. A token
            # on an unknown page, a modal, or an in-flight page is not completion.
            current = bridge.readiness()
            if not gate.observe(current) or before != gate.key:
                continue
            checkpoint("signup_home_ready", "ChatGPT 主页已可操作，正在连续确认登录态")
            if gate.confirm_identity():
                checkpoint("signup_identity_confirmed", "主页稳定且目标邮箱登录态已间隔确认两次")
                session = peeked
                checkpoint("signup_ready", "注册页面检查完成，等待官方入组核对后交接授权")
                break
        result = {"ok": bool(session), "signup_flow": "extension", "signup_version": version,
                  "signup_attempts": dict(state.attempts), "signup_retries": dict(state.retries),
                  "signup_diagnostics": {"schema": 1, "checkpoints": checkpoints,
                      "readiness": gate.diagnostics(), "workspace_actions": actions}}
        if session:
            payload = session["json"]
            result.update(access_token=onboard.session_access_token(session),
                          session_token=str(payload.get("sessionToken") or ""),
                          account_id=str(onboard.session_user(session).get("id") or ""), final_url=page.url)
        else:
            result.update(error_code=state.error_code or "registration_timeout",
                          error=state.message if state.error_code else "新版注册等待超时，请继续同一邮箱处理")
        return result
    except Exception:
        return {"ok": False, "signup_flow": "extension", "signup_version": version,
                "error_code": "registration_browser_failed",
                "signup_diagnostics": {"schema": 1, "checkpoints": checkpoints,
                    "readiness": gate.diagnostics(), "workspace_actions": actions},
                "error": "新版注册浏览器中断，请继续同一邮箱处理"}
    finally:
        try:
            if bridge is not None:
                bridge.close()
                if session:
                    checkpoint("signup_runner_stopped", "注册脚本及监听器已停止，可交接同一浏览器页面")
        finally:
            poller.close()
