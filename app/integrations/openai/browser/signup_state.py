"""Host-side contract for the shared signup content runner.

Only the CDP isolated-world bridge may call this object. State never lives in the
website, and navigation cannot reset submission/Continue budgets.
"""
from __future__ import annotations

import re
import time
import uuid
from urllib.parse import urlsplit

HOSTS = frozenset({"chatgpt.com", "auth.openai.com", "auth0.openai.com"})
STAGES = frozenset({"signup", "email", "password", "otp", "profile", "home", "unknown"})
LIMITS = {"signup": 3, "email": 2, "password": 2, "otp": 2, "profile": 2}
OUTCOMES = frozenset({"submitted", "loading", "advanced", "validation", "timeout"})
MESSAGES = {
    "signup": ("signup", "打开邮箱注册"), "email": ("fill_email", "填写邮箱"),
    "password": ("password", "填写注册密码"), "otp": ("email_otp", "填写邮箱验证码"),
    "profile": ("about_you", "填写注册资料"), "home": ("session", "核对登录身份"),
    "unknown": ("wait_page", "等待注册页面"),
}


def trusted_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return parsed.scheme == "https" and parsed.netloc in HOSTS
    except ValueError:
        return False


class SignupState:
    def __init__(self, *, email, password, profile, version, read_codes, baseline=(),
                 invite_entry=False, report=None, clock=time.monotonic):
        self.id = uuid.uuid4().hex
        self.email = email.strip().lower()
        self.password = password
        self.profile = dict(profile)
        self.version = version
        self.read_codes = read_codes
        self.used_codes = set(baseline)
        self.invite_entry = invite_entry
        self.report = report or (lambda *_: None)
        self.clock = clock
        self.started = clock()
        self.status = "running"
        self.stage = "unknown"
        self.error_code = ""
        self.message = "新版注册流程已启动"
        self.claims = {}
        self.attempts = {}
        self.reservations = {}
        self.receipts = {}
        self.retries = {}
        self.pending_code = None
        self.last_poll = -float("inf")
        self.entry_seen = False
        self.entry_fallback = False
        self.boundary_url = None
        self.events = []

    def fail(self, code, message):
        if self.status == "running":
            self.status, self.error_code, self.message = "paused", code, message
            self.report("manual_required", message)
        return {"active": False}

    def view(self):
        state = {"id": self.id, "active": self.status == "running", "status": self.status,
                 "stage": self.stage, "message": self.message, "email": self.email,
                 "mode": "auto", "resumeCount": 0, "entryFormSeen": self.entry_seen,
                 "entryFallbackUsed": self.entry_fallback}
        if state["active"]:
            state.update(password=self.password, profile=dict(self.profile))
        return state

    def event(self, event, stage, outcome=None):
        entry = {"event": event, "stage": stage}
        if outcome in OUTCOMES:
            entry["outcome"] = outcome
        self.events.append(entry)
        del self.events[:-200]

    def handle(self, message, url):
        return {"ok": True, **self._handle(message, url)}

    def _handle(self, message, url):
        if not isinstance(message, dict) or not trusted_url(url) or message.get("version") != self.version:
            return {"active": False}
        kind, stage = message.get("type"), message.get("stage")
        if kind != "state" and message.get("jobId") != self.id:
            return {"active": False}
        if self.clock() - self.started > 600:
            self.fail("registration_timeout", "注册超过十分钟，请继续同一邮箱处理")
        if kind == "state":
            return self.view()
        parsed = urlsplit(url)
        key = (parsed.netloc, parsed.path, stage)
        if kind == "finish-click":
            reserved = self.reservations.get(key)
            if not reserved or reserved["token"] != message.get("token") or type(message.get("sent")) is not bool:
                return {"accepted": False}
            del self.reservations[key]
            if not message["sent"]:
                self.claims.pop(key, None)
                self.attempts[stage] = max(0, self.attempts.get(stage, 0) - 1)
                if reserved["retry"]:
                    if reserved["previous"] is not None:
                        self.claims[key] = reserved["previous"]
                    self.retries[stage] -= 1
            else:
                if reserved["code"] and reserved["code"] == self.pending_code:
                    self.used_codes.add(self.pending_code)
                    self.pending_code = None
                self.receipts[key] = {"token": reserved["token"], "at": self.clock(),
                                      "blocked": self.receipts.get(key, {}).get("blocked", False)}
                self.event("submit_attempt", stage)
                if reserved["retry"]:
                    self.event("continue_retry", stage)
                    self.report(*MESSAGES[stage][:1], "Continue 无响应，已有限补点")
            return {"accepted": True}
        if self.status != "running":
            return {"active": False}
        if kind == "event":
            event = message.get("event")
            if stage not in STAGES:
                return {}
            if event == "page":
                self.stage = stage
                self.entry_seen |= stage in {"email", "password", "otp", "profile"}
                for receipt_key, receipt in self.receipts.items():
                    if receipt_key != key:
                        receipt["blocked"] = True
                self.report(*MESSAGES[stage])
            if event == "click_response" and message.get("outcome") in {"submitted", "loading", "advanced", "validation"}:
                if key in self.receipts:
                    self.receipts[key]["blocked"] = True
            failures = {"phone": ("phone_verification_required", "注册阶段要求手机验证，请继续同一邮箱处理"),
                        "captcha": ("cloudflare_challenge", "注册遇到人机验证，请人工处理"),
                        "rate_limit": ("openai_rate_limited", "注册页面提示限流，请稍后继续同一邮箱")}
            if event in failures:
                self.fail(*failures[event])
            if event in {"page", "filled", "form_submit", "click_response", "session_check", "session_error", *failures}:
                self.event(event, stage, message.get("outcome"))
            return {}
        if kind == "pause":
            # Page errors may contain account details; persist only bounded host messages.
            return self.fail("registration_manual_required", "新版注册流程已暂停，请检查页面并继续同一邮箱")
        if kind == "managed-page" and parsed.netloc == "chatgpt.com" and stage in {"home", "unknown"}:
            self.boundary_url = url
            return {}
        if kind == "complete":
            if parsed.netloc != "chatgpt.com":
                return {"active": False}
            if message.get("email", "").strip().lower() != self.email:
                return self.fail("token_identity_mismatch", "登录态邮箱与任务不一致")
            if message.get("verified") is False:
                return self.fail("email_unverified", "邮箱尚未验证，请继续同一邮箱完成验证")
            # The orchestrator independently reads the session before committing success.
            self.boundary_url = url
            return {}
        if kind == "entry-fallback":
            if self.invite_entry or parsed.netloc != "chatgpt.com" or parsed.path != "/" or self.entry_seen or self.entry_fallback or self.clock() - self.started < 15:
                return {"granted": False}
            self.entry_fallback = True
            return {"granted": True, "url": "https://chatgpt.com/auth/login"}
        if kind == "code":
            if self.stage != "otp" or key[:2] + ("otp",) in self.claims:
                return {"code": None}
            if self.pending_code:
                return {"code": self.pending_code}
            if self.clock() - self.last_poll < 4:
                return {"code": None}
            self.last_poll = self.clock()
            try:
                self.pending_code = next((code for code in self.read_codes()
                                          if isinstance(code, str) and re.fullmatch(r"\d{6}", code)
                                          and code not in self.used_codes), None)
            except Exception:
                self.fail("mail_otp_failed", "读取邮箱验证码失败，请检查邮箱配置后继续同一邮箱")
            return {"code": self.pending_code}
        if kind not in {"claim", "retry-continue", "continue-observed"} or stage not in LIMITS:
            return {"granted": False}
        if kind == "continue-observed":
            if stage not in {"otp", "profile"} or key in self.reservations:
                return {"granted": False}
            token = uuid.uuid4().hex
            self.receipts[key] = {"token": token, "at": self.clock(), "blocked": False}
            return {"granted": True, "token": token}
        retry = kind == "retry-continue"
        if retry:
            receipt = self.receipts.get(key)
            if stage not in {"otp", "profile"} or not receipt or receipt["blocked"] or receipt["token"] != message.get("token") or key in self.reservations or self.retries.get(stage, 0) >= 2 or self.attempts.get(stage, 0) >= 3:
                return {"granted": False}
            if self.clock() - receipt["at"] < 2:
                return {"granted": False, "wait": True}
        else:
            if key in self.claims:
                if self.clock() - self.claims[key] > 45:
                    self.fail("registration_submit_timeout", "提交后页面未前进，请继续同一邮箱处理")
                return {"granted": False}
            if self.attempts.get(stage, 0) >= LIMITS[stage]:
                self.fail("registration_attempt_limit", "已达到本步提交上限，请继续同一邮箱处理")
                return {"granted": False}
            if message.get("prepare") is True:
                return {"granted": True}
            if message.get("reserve") is not True:
                return {"granted": False}
        token = uuid.uuid4().hex
        self.reservations[key] = {"token": token, "previous": self.claims.get(key), "retry": retry,
                                  "code": self.pending_code if stage == "otp" else None}
        self.claims[key] = self.clock()
        self.attempts[stage] = self.attempts.get(stage, 0) + 1
        if retry:
            self.retries[stage] = self.retries.get(stage, 0) + 1
        return {"granted": True, "token": token}
