"""用 Playwright 跑完 OpenAI OAuth，给 iCloud 子号自动重新授权。"""
from __future__ import annotations

import logging
from pathlib import Path
import re
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from app.core.config import load_settings
from app.integrations.openai.browser import environment as browser_env
from app.integrations.openai.browser.chatgpt_login import (
    complete_chatgpt_onboarding,
    ensure_chatgpt_login,
    looks_like_chatgpt_onboarding,
)
from app.integrations.openai.browser.environment import BrowserEnvironmentError
from app.integrations.openai.browser.humanize import (
    human_click,
    human_nudge_mouse,
    human_pause,
    human_type_otp,
    human_type_text,
    uniform_float,
)
from app.integrations.openai.browser.onboard import (
    ABOUT_YOU_STUCK_LIMIT,
    NOT_REGISTERED_ERROR,
    _accept_terms,
    _click_exact,
    _click_first,
    _fill_about_you,
    _fill_first,
    _fill_phone_number,
    _submit_phone_sms,
    _find_otp,
    _mail_kwargs,
    _otp_boxes,
    _page_text,
    _pick_workspace,
    _save_debug,
    _snapshot_mailbox_codes,
    _visible,
    _wait_and_fallback_submit_otp,
    goto_with_retries,
    looks_like_about_you,
    looks_like_registration,
    looks_like_session_ended,
    page_is_cloudflare,
    page_phone_rejection,
    phone_page_outcome,
    record_pool_phone,
    release_pool_phone,
    split_phone,
    take_pool_phone,
    wait_cloudflare,
    wait_email_otp_with_resend,
)
from app.integrations.openai.chatgpt import is_desktop_authorize_url
from app.domain.reauth import is_oauth_callback, looks_like_deactivated
from app.integrations.proxy.socks_bridge import chrome_proxy_launch
from app.integrations.sms.client import require_proxy, sms_client

settings = load_settings()

logger = logging.getLogger(__name__)

StageCallback = Optional[Callable[[str, str], None]]


_SINGLE_TEAM_PICKER_JS = """() => {
  const vis = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity) === 0) return false;
    const box = el.getBoundingClientRect();
    return box.width > 8 && box.height > 8;
  };
  const formInputs = document.querySelectorAll(
    'input[type="email"], input[name="email"], input[name="username"], ' +
    'input[type="password"], input[type="tel"], input[name="phone"], ' +
    'input[autocomplete="one-time-code"], input[name="code"], input[name="otp"], input[name="pin"], input[maxlength="1"]'
  );
  for (const inp of formInputs) {
    if (vis(inp)) return { kind: "none" };
  }
  for (const old of document.querySelectorAll("[data-team48-ws-pick]")) {
    old.removeAttribute("data-team48-ws-pick");
  }
  const path = String(location.pathname || "").toLowerCase();
  const body = String((document.body && document.body.innerText) || "");
  const bodyLow = body.toLowerCase();
  const isChooser = /workspace|organization/.test(path) ||
    /choose a workspace|select a workspace|choose an organization|select an organization|选择工作空间|选择一个工作空间|选择组织/.test(bodyLow);
  const maybeWorkspacePage = isChooser || path.includes("consent") || bodyLow.includes("workspace") || body.includes("工作空间");
  if (!maybeWorkspacePage) return { kind: "none" };

  const norm = (el) => String(el.innerText || el.getAttribute("aria-label") || el.value || "").replace(/\\s+/g, " ").trim();
  const skipExact = new Set([
    "continue", "confirm", "allow", "authorize", "accept", "next", "ok", "okay", "submit",
    "join", "join workspace", "accept invite", "cancel", "deny", "decline", "back", "go back", "close",
    "log in", "login", "sign in", "sign up", "log out", "logout", "sign out",
    "switch account", "use another account", "use a different account",
    "sign in with another account", "log in with another account",
    "try again", "learn more", "help", "terms of use", "privacy policy",
    "继续", "确认", "允许", "授权", "接受", "下一步", "取消", "拒绝", "返回", "登录", "退出", "退出登录", "切换账号", "使用其他账号"
  ]);
  const skipSub = [
    "continue with ", "sign in with ", "log in with ", "use another", "use a different",
    "switch account", "log out", "sign out", "create a workspace", "create workspace", "new workspace",
    "privacy policy", "terms of use"
  ];
  const isActionOrChrome = (text) => {
    const low = text.toLowerCase();
    if (!low || low.includes("@")) return true;
    if (skipExact.has(low)) return true;
    return skipSub.some((sub) => low.includes(sub));
  };
  const isPersonal = (text) => text.toLowerCase().includes("personal") || text.includes("个人");

  let candidates = [];
  for (const r of document.querySelectorAll('input[type="radio"]')) {
    const target = r.closest("label") || r.closest('[role="radio"]') || (vis(r) ? r : null);
    if (target && vis(target) && !candidates.includes(target)) candidates.push(target);
  }
  for (const el of document.querySelectorAll('[role="radio"], [role="option"], [role="menuitemradio"]')) {
    if (!vis(el)) continue;
    if (candidates.some((c) => c === el || c.contains(el) || el.contains(c))) continue;
    candidates.push(el);
  }
  let maxLen = 160;
  if (candidates.length === 0) {
    maxLen = 80;
    const raw = Array.from(document.querySelectorAll('button, [role="listitem"], [role="button"]')).filter(
      (el) => vis(el) && !el.disabled && el.getAttribute("aria-disabled") !== "true"
    );
    candidates = raw.filter((el) => !raw.some((other) =>
      other !== el && (el.tagName === "BUTTON" ? other.tagName === "BUTTON" && el.contains(other)
                       : (el.contains(other) || (other.tagName === "BUTTON" && other.contains(el))))
    ));
  }

  const valid = [];
  for (const el of candidates) {
    const text = norm(el);
    if (!text || text.length > maxLen || isActionOrChrome(text)) continue;
    valid.push({ el, personal: isPersonal(text) });
  }
  if (valid.length === 0) return { kind: isChooser ? "wait" : "none" };
  const teams = valid.filter((item) => !item.personal);
  if (teams.length === 0) return { kind: "team_missing" };
  if (teams.length > 1) return { kind: "workspace_ambiguous" };
  teams[0].el.setAttribute("data-team48-ws-pick", "1");
  return { kind: "pick" };
}"""


def _pick_single_team_workspace(page) -> str:
    """Inspect a workspace chooser / consent page and select the unique non-personal Team."""
    try:
        outcome = page.evaluate(_SINGLE_TEAM_PICKER_JS) or {}
    except Exception:  # noqa: BLE001
        return "none"
    kind = str(outcome.get("kind") or "none")
    if kind != "pick":
        return kind
    try:
        target = page.locator('[data-team48-ws-pick="1"]').first
        if target.count() == 0 or not target.is_visible():
            return "none"
        human_pause(page, 0.8, 1.8)
        if not human_click(page, target, timeout=2500):
            return "none"
    except Exception:  # noqa: BLE001
        return "none"
    human_pause(page, 0.6, 1.2)
    if not _click_exact(page, ["Continue", "Confirm", "Allow", "Authorize", "Accept", "继续", "确认", "允许", "授权"]):
        _click_first(page, [
            'button:has-text("Continue")', 'button:has-text("Confirm")', 'button:has-text("Allow")',
            'button:has-text("继续")', 'button:has-text("确认")', 'button:has-text("允许")',
        ])
    return "picked"


_ACCOUNT_PICKER_JS = """(targetEmail) => {
  const wanted = String(targetEmail || "").trim().toLowerCase();
  if (!wanted) return { kind: "none" };
  const vis = (el) => {
    if (!el) return false;
    const st = window.getComputedStyle(el);
    if (st.display === "none" || st.visibility === "hidden" || parseFloat(st.opacity) === 0) return false;
    const box = el.getBoundingClientRect();
    return box.width > 8 && box.height > 8;
  };
  const formInputs = document.querySelectorAll(
    'input[type="email"], input[name="email"], input[name="username"], ' +
    'input[type="password"], input[type="tel"], input[name="phone"], ' +
    'input[autocomplete="one-time-code"], input[name="code"], input[name="otp"], input[name="pin"], input[maxlength="1"]'
  );
  for (const inp of formInputs) {
    if (vis(inp)) return { kind: "none" };
  }
  for (const old of document.querySelectorAll("[data-team48-account-pick]")) {
    old.removeAttribute("data-team48-account-pick");
  }
  const path = String(location.pathname || "").toLowerCase();
  const body = String((document.body && document.body.innerText) || "");
  const bodyLow = body.toLowerCase();
  const isWorkspaceChooser = /workspace|organization/.test(path) ||
    /choose a workspace|select a workspace|choose an organization|select an organization|选择工作空间|选择一个工作空间|选择组织/.test(bodyLow);
  if (isWorkspaceChooser) return { kind: "none" };

  const norm = (el) => String(el.innerText || el.getAttribute("aria-label") || el.value || "").replace(/\\s+/g, " ").trim();
  const emailRe = /[a-z0-9._%+-]+@[a-z0-9.-]+\\.[a-z]{2,}/gi;
  const extractEmails = (text) => Array.from(new Set((String(text || "").toLowerCase().match(emailRe) || [])));
  const forbidden = [
    "use another", "use a different", "switch account", "change account",
    "log out", "logout", "sign out", "signout", "not you",
    "使用其他账号", "切换账号", "换个账号", "退出登录", "退出"
  ];
  const isForbidden = (text) => {
    const low = text.toLowerCase();
    return forbidden.some((sub) => low.includes(sub));
  };

  const raw = Array.from(document.querySelectorAll(
    'button, a, [role="button"], [role="option"], [role="radio"], [role="listitem"], [role="menuitem"], [role="menuitemradio"], label'
  )).filter((el) =>
    vis(el) && !el.disabled && el.getAttribute("aria-disabled") !== "true" &&
    !el.closest("nav, footer") && el.getAttribute("aria-haspopup") !== "menu"
  );
  const withEmail = raw.filter((el) => {
    const text = norm(el);
    return text && text.length <= 240 && !isForbidden(text) && extractEmails(text).length > 0;
  });
  const candidates = withEmail.filter((el) => !withEmail.some((other) => other !== el && el.contains(other)));

  let targetEl = null;
  const otherEmails = new Set();
  for (const el of candidates) {
    const emails = extractEmails(norm(el));
    if (emails.includes(wanted)) {
      if (!targetEl) targetEl = el;
    } else {
      for (const em of emails) {
        if (!em.endsWith("@openai.com")) otherEmails.add(em);
      }
    }
  }
  if (targetEl) {
    targetEl.setAttribute("data-team48-account-pick", "1");
    return { kind: "pick" };
  }
  if (otherEmails.size > 0) {
    return { kind: "identity_mismatch" };
  }
  const isAccountPage = /desktop-auth|select-account|choose-account|account-selection|accounts/.test(path) ||
    /choose an account|select an account|pick an account|continue as|sign in as|选择账号|选择一个账号|以此账号继续/.test(bodyLow);
  if (isAccountPage) {
    const pageEmails = extractEmails(body).filter((em) => !em.endsWith("@openai.com"));
    if (pageEmails.length > 0 && !pageEmails.includes(wanted)) {
      return { kind: "identity_mismatch" };
    }
  }
  return { kind: "none" };
}"""

_INVALID_STATE_409_RE = re.compile(
    r"(?:\b(?:http|status|error|code)\s*[:#]?\s*409\b|\b409\s*(?:conflict|error)\b|\(\s*409\s*\))",
    re.IGNORECASE,
)
_AUTH_LOGIN_HOSTS = frozenset({"auth.openai.com", "auth0.openai.com"})


def _pick_account_card(page, email: str) -> str:
    """Select the account card matching ``email``; returns 'picked', 'identity_mismatch', or 'none'."""
    try:
        outcome = page.evaluate(_ACCOUNT_PICKER_JS, email) or {}
    except Exception:  # noqa: BLE001
        return "none"
    kind = str(outcome.get("kind") or "none")
    if kind != "pick":
        return kind
    started_url = (page.url or "").split("#", 1)[0]
    try:
        target = page.locator('[data-team48-account-pick="1"]').first
        if target.count() == 0 or not target.is_visible():
            return "none"
        human_pause(page, 0.8, 1.8)
        if not human_click(page, target, timeout=2500):
            return "none"
    except Exception:  # noqa: BLE001
        return "none"
    human_pause(page, 0.8, 1.4)
    try:
        current_url = (page.url or "").split("#", 1)[0]
        if current_url == started_url and target.count() > 0 and target.is_visible():
            _click_exact(page, ["Continue", "Confirm", "Allow", "Authorize", "继续", "确认", "允许", "授权"], pre_pause=(0.5, 1.1))
    except Exception:  # noqa: BLE001
        pass
    return "picked"


def looks_like_invalid_state(*, title: str = "", body: str = "", url: str = "", email: str = "") -> bool:
    blob = f"{title}\n{body}\n{url}".lower()
    if email:
        blob = blob.replace(email.strip().lower(), "")
    if "invalid_state" in blob or "invalid state" in blob:
        return True
    if _INVALID_STATE_409_RE.search(blob):
        return True
    if re.search(r"\b409\b", blob) and any(
        token in blob
        for token in ("something went wrong", "oops", "conflict", "try again", "unable to", "出错", "错误", "失败", "invalid")
    ):
        return True
    return False


def _has_visible_input(page, selector: str) -> bool:
    try:
        loc = page.locator(selector)
        for index in range(loc.count()):
            el = loc.nth(index)
            if el.is_visible() and (el.get_attribute("type") or "").lower() != "hidden":
                return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _looks_like_auth_login_prompt(
    page,
    *,
    url: str,
    title: str,
    body: str,
    password_boxes: list,
    on_phone: bool,
) -> bool:
    """True when desktop-auth falls back to asking for email/password/OTP on auth.openai.com."""
    if on_phone:
        return False
    url_low = (url or "").lower()
    host = (urlparse(url or "").hostname or "").lower()
    on_auth_host = host in _AUTH_LOGIN_HOSTS
    on_chatgpt_login = host in {"chatgpt.com", "www.chatgpt.com"} and (
        "/auth/login" in url_low or "/codex/desktop-auth" in url_low
    )
    if not (on_auth_host or on_chatgpt_login):
        return False
    if _has_visible_input(page, 'input[type="email"], input[name="email"], input[name="username"]'):
        return True
    if password_boxes:
        return True
    body_low = (body or "").lower()
    if bool(_find_otp(page) or _otp_boxes(page)) or "email-verification" in url_low or "check your inbox" in body_low:
        return True
    if on_auth_host and looks_like_session_ended(title=title, body=body):
        return True
    if on_auth_host and _visible(page, 'button:has-text("Continue with email")'):
        return True
    return False


def _visible_password_inputs(page) -> list:
    boxes = page.locator('input[type="password"]')
    visible = []
    for index in range(boxes.count()):
        try:
            box = boxes.nth(index)
            if box.is_visible():
                visible.append(box)
        except Exception:  # noqa: BLE001
            continue
    return visible


def _signup_dwell_bounds() -> tuple[float, float]:
    raw_min = getattr(settings, "signup_oauth_dwell_min_sec", 15)
    raw_max = getattr(settings, "signup_oauth_dwell_max_sec", 30)
    try:
        lo = max(0.0, float(raw_min))
    except (TypeError, ValueError):
        lo = 15.0
    try:
        hi = max(0.0, float(raw_max))
    except (TypeError, ValueError):
        hi = 30.0
    if hi < lo:
        hi = lo
    return lo, hi


def run_browser_oauth_reauth(
    *,
    email: str,
    password: str,
    authorize_url: str,
    proxy: str,
    pickup_url: str = "",
    phone: str = "",
    sms_url: str = "",
    use_cloudflare: bool = False,
    cf_base_url: str = "",
    cf_address: str = "",
    cf_admin_password: str = "",
    hme_base_url: str = "",
    hme_service_token: str = "",
    hme_account_id: str = "",
    allow_signup: bool = False,
    login_only: bool = False,
    allow_profile_setup: bool = False,
    allow_join: bool = True,
    allow_onboarding: bool | None = None,
    allow_sms: bool = True,
    max_sms_submissions: int = 7,
    max_sms_code_submissions: int = 2,
    team_name: str = "",
    pick_single_team: bool = False,
    executable_path: str = "",
    on_stage: StageCallback = None,
    phone_source=None,
    existing_browser=None,
    existing_page=None,
) -> Dict[str, Any]:
    require_proxy(proxy, "子号浏览器")
    if not allow_sms:
        phone, sms_url, phone_source = "", "", None
    if not authorize_url:
        return {"ok": False, "error": "缺少授权链接", "error_code": "oauth_url_missing"}

    effective_onboarding = bool(allow_join) if allow_onboarding is None else bool(allow_onboarding)

    def report(stage: str, message: str) -> None:
        if on_stage:
            try:
                on_stage(stage, message)
            except Exception:  # noqa: BLE001
                logger.debug("on_stage failed", exc_info=True)

    profile_dir = (
        Path(__file__).resolve().parents[4]
        / "data"
        / "chrome-profiles"
        / email.replace("@", "_at_")
    )
    profile_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ok": False, "email": email, "callback_url": "", "phone": phone, "sms_url": sms_url}
    captured = {"url": ""}
    observed_409 = {"seen": False, "invalid_state": False}
    desktop_flow = is_desktop_authorize_url(authorize_url)

    def remember(url: str) -> None:
        if is_oauth_callback(url):
            captured["url"] = url

    def fulfill_callback(route) -> None:
        remember(route.request.url)
        route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body="<html><body>ok</body></html>",
        )

    def on_response(resp) -> None:
        try:
            if resp.status != 409:
                return
            resp_url = (resp.url or "").lower()
            if (
                "auth.openai.com" not in resp_url
                and "auth0.openai.com" not in resp_url
                and "chatgpt.com/codex/desktop-auth" not in resp_url
            ):
                return
            observed_409["seen"] = True
            try:
                if "invalid_state" in (resp.text() or "").lower():
                    observed_409["invalid_state"] = True
            except Exception:  # noqa: BLE001
                pass
        except Exception:  # noqa: BLE001
            pass

    from contextlib import ExitStack

    with ExitStack() as stack:
        if existing_browser is not None:
            browser, page = existing_browser, existing_page
        else:
            proxy_config = stack.enter_context(chrome_proxy_launch(proxy))
            try:
                browser, page, actual_profile_dir, summary = stack.enter_context(
                    browser_env.launch_browser_context(
                        profile_dir,
                        proxy_config,
                        settings=settings,
                        executable_path=executable_path,
                    )
                )
            except BrowserEnvironmentError as exc:
                release_pool_phone(phone_source)
                return {**result, "error_code": exc.error_code, "error": str(exc)}
            profile_dir = Path(actual_profile_dir)
            report("browser_environment", summary)
        page.set_default_timeout(60000)

        def attach(target) -> None:
            try:
                target.route("http://localhost:1455/**", fulfill_callback)
                target.route("http://127.0.0.1:1455/**", fulfill_callback)
                target.on("framenavigated", lambda frame: remember(frame.url or ""))
                target.on("response", on_response)
            except Exception:  # noqa: BLE001
                pass

        try:
            try:
                browser.route("http://localhost:1455/**", fulfill_callback)
                browser.route("http://127.0.0.1:1455/**", fulfill_callback)
            except Exception:  # noqa: BLE001
                pass
            browser.on("page", attach)
            for existing in list(browser.pages):
                attach(existing)

            sms_code_submissions = 0
            if desktop_flow:
                login_res = ensure_chatgpt_login(
                    page,
                    email=email,
                    password=password,
                    pickup_url=pickup_url,
                    proxy=proxy,
                    use_cloudflare=use_cloudflare,
                    cf_base_url=cf_base_url,
                    cf_address=cf_address,
                    cf_admin_password=cf_admin_password,
                    hme_base_url=hme_base_url,
                    hme_service_token=hme_service_token,
                    hme_account_id=hme_account_id,
                    allow_profile_setup=allow_profile_setup,
                    allow_onboarding=effective_onboarding,
                    team_name=team_name,
                    pick_single_team=pick_single_team,
                    allow_sms=allow_sms,
                    phone=phone,
                    sms_url=sms_url,
                    phone_source=phone_source,
                    on_stage=report,
                )
                phone = str(login_res.get("phone", phone) or "")
                sms_url = str(login_res.get("sms_url", sms_url) or "")
                sms_code_submissions += int(login_res.get("sms_code_submissions") or 0)
                if not login_res.get("ok"):
                    result["error"] = login_res.get("error") or "ChatGPT 登录未完成"
                    result["error_code"] = login_res.get("error_code") or "browser_failed"
                    result["final_url"] = page.url or ""
                    result["debug_dir"] = _save_debug(page, profile_dir)
                    return result

                if login_res.get("created_account"):
                    dwell_min, dwell_max = _signup_dwell_bounds()
                    dwell_sec = uniform_float(dwell_min, dwell_max)
                    if dwell_sec > 0:
                        report("post_signup_dwell", f"已进入团队主页，停留 {int(round(dwell_sec))} 秒后发起授权")
                        deadline = time.monotonic() + dwell_sec
                        target_nudges = 1 if dwell_sec < 8.0 else (1 if uniform_float(0.0, 1.0) < 0.5 else 2)
                        nudges_done = 0
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            chunk = min(remaining, uniform_float(3.0, 5.0))
                            page.wait_for_timeout(int(round(chunk * 1000.0)))
                            if effective_onboarding and looks_like_chatgpt_onboarding(page):
                                ob = complete_chatgpt_onboarding(
                                    page,
                                    team_name=team_name,
                                    pick_single_team=pick_single_team,
                                    target_email=email,
                                    report=report,
                                    initial_wait=False,
                                )
                                if not ob.get("ok"):
                                    result["error"] = ob.get("error") or "卡在 ChatGPT 入组欢迎页，未能自动进入工作空间"
                                    result["error_code"] = ob.get("error_code") or "onboarding_stuck"
                                    result["final_url"] = page.url or ""
                                    result["debug_dir"] = _save_debug(page, profile_dir)
                                    return result
                            if nudges_done < target_nudges and (deadline - time.monotonic()) > 0.8:
                                human_nudge_mouse(page)
                                nudges_done += 1
                else:
                    human_pause(page, 1.8, 3.5)

            report("browser_open", "正在打开 Codex 授权链接")
            goto_with_retries(page, authorize_url, report=report)
            if not wait_cloudflare(page):
                result["error"] = "遇到 Cloudflare 人机验证，未自动通过"
                result["error_code"] = "cloudflare_challenge"
                result["final_url"] = page.url or ""
                result["debug_dir"] = _save_debug(page, profile_dir)
                return result
            human_pause(page, 1.2, 2.2)
            remember(page.url or "")
            known_codes = set() if desktop_flow else _snapshot_mailbox_codes(
                **_mail_kwargs(
                    email=email,
                    pickup_url=pickup_url,
                    proxy=proxy,
                    use_cloudflare=use_cloudflare,
                    cf_base_url=cf_base_url,
                    cf_address=cf_address,
                    cf_admin_password=cf_admin_password,
                    hme_base_url=hme_base_url,
                    hme_service_token=hme_service_token,
                    hme_account_id=hme_account_id,
                )
            )
            otp_submits = 0
            phone_tries = 0
            sms_submissions = 0
            otp_resends = 0
            password_tried = False
            signup_clicked = False
            about_you_tries = 0
            account_pick_url = ""
            account_pick_count = 0

            for _ in range(36):
                remember(page.url or "")
                if captured["url"]:
                    break

                if page_is_cloudflare(page) and not wait_cloudflare(page, timeout_sec=30):
                    result["error"] = "遇到 Cloudflare 人机验证，未自动通过"
                    result["error_code"] = "cloudflare_challenge"
                    report("cloudflare_challenge", result["error"])
                    break

                url = (page.url or "").lower()
                page_title = page.title() or ""
                page_body = _page_text(page)
                if looks_like_deactivated(title=page_title, body=page_body, url=page.url or ""):
                    result["error"] = "账号已被 deactivate，停止重授权"
                    result["error_code"] = "account_deactivated"
                    report("deactivated", result["error"])
                    break
                if observed_409["invalid_state"] or looks_like_invalid_state(
                    title=page_title, body=page_body, url=page.url or "", email=email
                ):
                    result["error"] = "OpenAI 授权页返回 409 / invalid_state，已停止"
                    result["error_code"] = "oauth_invalid_state"
                    report("oauth_invalid_state", result["error"])
                    break
                if login_only and not allow_profile_setup and (
                    looks_like_registration(url=url, body=page_body)
                    or looks_like_about_you(title=page_title, body=page_body, url=url)
                ):
                    result["error"] = NOT_REGISTERED_ERROR
                    result["error_code"] = "account_not_registered"
                    report("not_registered", result["error"])
                    break
                if looks_like_about_you(title=page_title, body=page_body, url=url):
                    about_you_tries += 1
                    if about_you_tries > ABOUT_YOU_STUCK_LIMIT:
                        result["error"] = "卡在年龄/生日页，没有进入下一步"
                        result["error_code"] = "about_you_stuck"
                        break
                    report("about_you", f"验证码已过，正在填写年龄（第 {about_you_tries} 次）")
                    if not _fill_about_you(page, email=email):
                        human_pause(page, 1.2, 2.0)
                    continue
                if not desktop_flow and looks_like_session_ended(title=page_title, body=page_body):
                    report("session_ended", "授权页会话结束，点 Log in")
                    _click_first(page, ['button:has-text("Log in")', 'a:has-text("Log in")'], pre_pause=(0.6, 1.3))
                    human_pause(page, 1.5, 2.5)
                    continue
                if "you're all set" in page_body.lower() or "you’re all set" in page_body.lower():
                    report("all_set", "注册完成页，点 Continue")
                    _click_first(page, ['button:has-text("Continue")'], pre_pause=(0.8, 1.8))
                    human_pause(page, 1.5, 2.5)
                    continue
                if not allow_join:
                    on_invite_action = "accept-invite" in url or any(_visible(page, selector) for selector in (
                        'button:has-text("Accept invite")', 'button:has-text("Join workspace")',
                        'button:has-text("接受邀请")', 'button:has-text("加入工作空间")',
                    ))
                    if on_invite_action:
                        if effective_onboarding:
                            report("onboarding", "正在接受团队邀请进入工作空间")
                            clicked_invite = (
                                _click_exact(
                                    page,
                                    ["Accept invite", "Join workspace", "Accept", "接受邀请", "加入工作空间"],
                                    pre_pause=(0.8, 1.8),
                                )
                                or _click_first(
                                    page,
                                    [
                                        'button:has-text("Accept invite")',
                                        'button:has-text("Join workspace")',
                                        'button:has-text("接受邀请")',
                                        'button:has-text("加入工作空间")',
                                    ],
                                    pre_pause=(0.8, 1.8),
                                )
                            )
                            human_pause(page, 1.8, 2.8) if clicked_invite else human_pause(page, 1.2, 2.0)
                            continue
                        result.update(error="账号需要接受邀请，重授权不会自动入组，请人工核对", error_code="membership_changed")
                        break
                    if team_name and _click_exact(page, [team_name], pre_pause=(0.8, 1.8)):
                        human_pause(page, 0.5, 1.1)
                        _click_exact(page, ["Continue", "Confirm", "继续", "确认"])
                        report("workspace", "已选择原工作空间")
                        human_pause(page, 1.4, 2.4)
                        continue
                    if pick_single_team and not team_name:
                        ws_outcome = _pick_single_team_workspace(page)
                        if ws_outcome == "picked":
                            report("workspace", "已自动选择唯一 Team 工作空间")
                            human_pause(page, 1.6, 2.6)
                            continue
                        if ws_outcome == "wait":
                            human_pause(page, 1.2, 2.0)
                            continue
                        if ws_outcome == "workspace_ambiguous":
                            result.update(error="有多个 Team，未自动选择", error_code="workspace_ambiguous")
                            report("workspace", result["error"])
                            break
                        if ws_outcome == "team_missing":
                            result.update(error="只有个人空间，没有 Team", error_code="team_missing")
                            report("workspace", result["error"])
                            break
                if allow_join and _pick_workspace(page, team_name):
                    report("workspace", "已选择工作空间")
                    human_pause(page, 1.4, 2.4)
                    continue
                on_phone = any(bit in url for bit in ("add-phone", "phone-verification")) or _visible(page, 'input[type="tel"]')
                if on_phone:
                    if not allow_sms:
                        result["error"] = "本次流程不自动接短信，页面要求手机验证，已停止"
                        result["error_code"] = "phone_verification_required"
                        report("manual_required", result["error"])
                        break
                    report("add_phone", "授权页要求手机号/短信验证码")
                    outcome, msg = phone_page_outcome(page, allow_risk=False)
                    if outcome in {"invalid", "recently_used", "risk"}:
                        if phone_source:
                            record_pool_phone(phone_source, outcome, msg)
                            report("add_phone", f"换号原因：{msg}")
                            number, next_sms_url, error, code = take_pool_phone(phone_source, report)
                            if error:
                                result["error"] = error
                                result["error_code"] = code
                                break
                            phone, sms_url = number, next_sms_url
                            continue
                        result["error"] = msg or page_phone_rejection(page) or "手机号不被 OpenAI 接受"
                        result["error_code"] = "sms_rejected"
                        break
                    if not phone or not sms_url:
                        if phone_source:
                            number, next_sms_url, error, code = take_pool_phone(phone_source, report)
                            if error:
                                result["error"] = error
                                result["error_code"] = code
                                break
                            phone, sms_url = number, next_sms_url
                        elif _click_exact(page, ["Skip", "Not now", "Maybe later", "Skip for now", "I'll do this later"], pre_pause=(0.6, 1.3)):
                            report("add_phone", "手机号页已跳过")
                            human_pause(page, 1.2, 2.0)
                            continue
                        else:
                            result["error"] = "需要接码，但未提供手机号"
                            result["error_code"] = "sms_missing"
                            break
                    phone_tries += 1
                    if phone_tries >= 8:
                        result["error"] = "卡在手机号页，没能发出短信"
                        result["error_code"] = "sms_failed"
                        break
                    if _visible(page, 'input[type="tel"]'):
                        if sms_submissions >= max_sms_submissions:
                            result["error"] = "短信发送已达到本次上限，未重发或换号"
                            result["error_code"] = "sms_send_limit"
                            break
                        country, _national = split_phone(phone)
                        filled = _fill_phone_number(page, phone)
                        if not filled and country == "China":
                            msg = "接码是 +86，但授权页停在美国 +1。Codex 不吃这个号，换能过的 +1 接码"
                            if phone_source:
                                record_pool_phone(phone_source, "invalid", msg)
                                report("add_phone", f"换号原因：{msg}")
                                number, next_sms_url, error, code = take_pool_phone(phone_source, report)
                                if error:
                                    result["error"] = error
                                    result["error_code"] = code
                                    break
                                phone, sms_url = number, next_sms_url
                                continue
                            result["error"] = msg
                            result["error_code"] = "sms_rejected"
                            break
                        _submit_phone_sms(page)
                        sms_submissions += 1
                        human_pause(page, 2.0, 3.0)
                        outcome, msg = phone_page_outcome(page)
                        if outcome in {"invalid", "recently_used", "risk"}:
                            if phone_source:
                                record_pool_phone(phone_source, outcome, msg)
                                report("add_phone", f"换号原因：{msg}")
                                number, next_sms_url, error, code = take_pool_phone(phone_source, report)
                                if error:
                                    result["error"] = error
                                    result["error_code"] = code
                                    break
                                phone, sms_url = number, next_sms_url
                                continue
                            result["error"] = msg or page_phone_rejection(page)
                            result["error_code"] = "sms_rejected"
                            break
                        continue
                    otp_boxes = _otp_boxes(page)
                    otp_el = _find_otp(page)
                    if otp_boxes is not None or otp_el:
                        if sms_code_submissions >= max_sms_code_submissions:
                            result["error"] = "短信验证码未通过，未重复提交或换号"
                            result["error_code"] = "sms_code_rejected"
                            break
                        report("sms_otp", "等待短信验证码")
                        try:
                            sms_code = sms_client.wait_for_code(sms_url, proxy=proxy, timeout_sec=90)
                        except TimeoutError as exc:
                            msg = str(exc) or "接码超时或空号"
                            if phone_source:
                                record_pool_phone(phone_source, "no_sms", msg)
                                report("add_phone", f"换号原因：{msg}")
                                number, next_sms_url, error, code = take_pool_phone(phone_source, report)
                                if error:
                                    result["error"] = error
                                    result["error_code"] = code or "sms_failed"
                                    break
                                phone, sms_url = number, next_sms_url
                                continue
                            result["error"] = msg
                            result["error_code"] = "sms_failed"
                            break
                        human_pause(page, 0.8, 1.8)
                        started_url = page.url or ""
                        if human_type_otp(page, sms_code, single_input=otp_el, boxes=otp_boxes):
                            sms_code_submissions += 1
                            _wait_and_fallback_submit_otp(
                                page,
                                started_url=started_url,
                                exact_names=["Continue", "Verify"],
                                selectors=['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")'],
                            )
                        human_pause(page, 2.0, 3.0)
                        record_pool_phone(phone_source, "success")
                        result["sms_verified"] = not any(bit in (page.url or "").lower() for bit in ("add-phone", "phone-verification"))
                        result["phone"] = phone
                        result["sms_url"] = sms_url
                    else:
                        human_pause(page, 1.2, 2.0)
                    continue

                password_boxes = _visible_password_inputs(page)

                if desktop_flow:
                    base_url = (page.url or "").split("#", 1)[0]
                    if base_url != account_pick_url:
                        account_pick_url = base_url
                        account_pick_count = 0
                    if account_pick_count < 2:
                        acct_outcome = _pick_account_card(page, email)
                        if acct_outcome == "identity_mismatch":
                            result["error"] = "选账号页只出现其他邮箱，与目标账号不一致，已停止"
                            result["error_code"] = "identity_mismatch"
                            report("identity_mismatch", result["error"])
                            break
                        if acct_outcome == "picked":
                            account_pick_count += 1
                            report("account_pick", "已点选目标邮箱账号卡")
                            human_pause(page, 1.2, 2.2)
                            continue
                    if _looks_like_auth_login_prompt(
                        page,
                        url=page.url or "",
                        title=page_title,
                        body=page_body,
                        password_boxes=password_boxes,
                        on_phone=on_phone,
                    ):
                        result["error"] = "桌面版授权页未识别已登录会话，要求在授权页重新登录，已停止"
                        result["error_code"] = "desktop_session_missing"
                        report("desktop_session_missing", result["error"])
                        break
                    if _accept_terms(page):
                        report("terms", "已勾选服务条款")
                        human_pause(page, 1.2, 2.0)
                        continue
                    if _click_exact(
                        page,
                        [
                            "Allow",
                            "Accept",
                            "Authorize",
                            "Continue",
                            "Continue to Codex",
                            "Authorize Codex",
                            "I agree",
                            "Okay",
                            "Confirm",
                            "继续",
                            "允许",
                            "授权",
                            "确认",
                            "同意",
                        ],
                        pre_pause=(0.8, 1.8),
                    ):
                        human_pause(page, 1.2, 2.2)
                        continue
                    human_pause(page, 1.0, 1.6)
                    continue

                if allow_signup and password_boxes and not signup_clicked and (
                    _click_exact(page, ["Sign up", "Create account", "Create an account"], pre_pause=(0.6, 1.3))
                    or _click_first(page, ['a:has-text("Sign up")', 'button:has-text("Sign up")'], pre_pause=(0.6, 1.3))
                ):
                    signup_clicked = True
                    report("create_account", "密码页改走注册")
                    human_pause(page, 1.2, 2.0)
                    continue

                if allow_signup and not password_tried and (
                    _click_exact(page, ["Continue with password", "Try again"], pre_pause=(0.6, 1.3))
                    or _click_first(page, ['button:has-text("Continue with password")', 'button:has-text("Try again")'], pre_pause=(0.6, 1.3))
                ):
                    report("create_password", "授权注册需要密码，不走无密码验证码")
                    human_pause(page, 1.2, 2.0)
                    password_boxes = _visible_password_inputs(page)

                if allow_signup and password_boxes and not password_tried:
                    report("password", "正在设置注册密码")
                    human_pause(page, 0.8, 1.6)
                    for box in password_boxes:
                        try:
                            human_type_text(page, box, password, clear=True)
                        except Exception:  # noqa: BLE001
                            continue
                    human_pause(page, 0.5, 1.1)
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Create account")'])
                    password_tried = True
                    human_pause(page, 2.0, 3.0)
                    continue

                otp_boxes = _otp_boxes(page)
                otp_el = _find_otp(page)
                if otp_boxes is not None or otp_el:
                    if otp_submits >= 2:
                        result["error"] = "邮箱验证码提交后仍未通过，没有继续连交"
                        result["error_code"] = "mail_otp_rejected"
                        break
                    report("email_otp", "等待新的邮箱验证码" if otp_submits or otp_resends else "等待邮箱验证码")
                    code, otp_resends, otp_error = wait_email_otp_with_resend(
                        page,
                        email=email,
                        pickup_url=pickup_url,
                        proxy=proxy,
                        use_cloudflare=use_cloudflare,
                        cf_base_url=cf_base_url,
                        cf_address=cf_address,
                        cf_admin_password=cf_admin_password,
                        hme_base_url=hme_base_url,
                        hme_service_token=hme_service_token,
                        hme_account_id=hme_account_id,
                        ignore=known_codes,
                        resends=otp_resends,
                        report=report,
                        check_past_otp=True,
                    )
                    if otp_error:
                        result["error"] = otp_error
                        result["error_code"] = "mail_otp_timeout"
                        break
                    if not code:
                        continue
                    human_pause(page, 1.0, 2.0)
                    started_url = page.url or ""
                    if human_type_otp(page, code, single_input=otp_el, boxes=otp_boxes):
                        _wait_and_fallback_submit_otp(
                            page,
                            started_url=started_url,
                            exact_names=["Continue", "Verify"],
                            selectors=['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")'],
                        )
                    known_codes.add(code)
                    otp_submits += 1
                    report("email_otp", f"已提交验证码（第 {otp_submits} 次）")
                    if not captured["url"]:
                        human_pause(page, 2.0, 3.0)
                    continue

                on_verify = "email-verification" in url or "check your inbox" in page_body.lower()
                if on_verify and not password_boxes:
                    report("email_otp", "验证码页还没出现输入框，继续等")
                    human_pause(page, 1.2, 2.0)
                    continue

                if password_boxes:
                    if allow_signup and password_tried:
                        human_pause(page, 1.2, 2.0)
                        continue
                    body = page_body.lower()
                    missing_password = not str(password or "").strip()
                    bad_password = password_tried or missing_password or any(
                        token in body for token in ("incorrect", "wrong password", "did not match")
                    )
                    if (missing_password or bad_password) and (
                        _click_exact(
                            page,
                            ["Log in with a one-time code", "Email me a code", "Send a code", "Use a one-time code"],
                            pre_pause=(1.0, 2.2),
                        )
                        or _click_first(
                            page,
                            ['button:has-text("one-time code")', 'button:has-text("Email me a code")'],
                            pre_pause=(1.0, 2.2),
                        )
                    ):
                        report("email_otp", "没有本地密码，改走一次性验证码" if missing_password else "密码不对，改走一次性验证码")
                        human_pause(page, 2.0, 3.0)
                        continue
                    if missing_password:
                        result["error"] = "没有本地密码，也没有邮箱验证码入口"
                        result["error_code"] = "password_missing"
                        break
                    if bad_password:
                        result["error"] = "密码不对，也没有邮箱验证码入口"
                        result["error_code"] = "password_rejected"
                        break
                    report("password", "正在填写密码")
                    human_pause(page, 0.8, 1.6)
                    for box in password_boxes:
                        try:
                            human_type_text(page, box, password, clear=True)
                        except Exception:  # noqa: BLE001
                            continue
                    human_pause(page, 0.5, 1.1)
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Create account")'])
                    password_tried = True
                    human_pause(page, 2.0, 3.0)
                    continue

                if page.locator('input[type="email"], input[name="email"], input[name="username"]').count() > 0:
                    report("fill_email", "正在填写邮箱")
                    human_pause(page, 0.8, 1.8)
                    _fill_first(
                        page,
                        ['input[name="email"]', 'input[type="email"]', 'input[name="username"]'],
                        email,
                    )
                    human_pause(page, 0.5, 1.1)
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Next")'])
                    human_pause(page, 2.0, 3.0)
                    continue

                if _accept_terms(page):
                    report("terms", "已勾选服务条款")
                    human_pause(page, 1.2, 2.0)
                    continue

                if _click_first(
                    page,
                    [
                        'button:has-text("Continue with email")',
                        'button:has-text("Log in")',
                        'a:has-text("Log in")',
                    ],
                    pre_pause=(0.8, 1.6),
                ):
                    human_pause(page, 1.2, 2.0)
                    continue

                if _click_first(
                    page,
                    [
                        'button:has-text("Allow")',
                        'button:has-text("Accept")',
                        'button:has-text("Authorize")',
                        'button:has-text("Continue")',
                        'button:has-text("I agree")',
                        'button:has-text("Okay")',
                    ],
                    pre_pause=(0.8, 1.8),
                ):
                    human_pause(page, 1.2, 2.2)
                    continue
                human_pause(page, 1.0, 1.6)

            remember(page.url or "")
            if not captured["url"] and not result.get("error"):
                for _ in range(20):
                    page.wait_for_timeout(1000)
                    remember(page.url or "")
                    if captured["url"]:
                        break

            callback = captured["url"]
            result["sms_verified"] = bool(callback and sms_code_submissions)
            result.update({
                "ok": bool(callback),
                "callback_url": callback,
                "final_url": page.url or "",
            })
            if not callback:
                if not result.get("error_code"):
                    if observed_409["seen"] or looks_like_invalid_state(
                        title=page.title() or "", body=_page_text(page), url=page.url or "", email=email
                    ):
                        result["error"] = "OpenAI 授权页返回 409 / invalid_state"
                        result["error_code"] = "oauth_invalid_state"
                    elif page_is_cloudflare(page):
                        result["error"] = "遇到 Cloudflare 人机验证，未拿到 OAuth 回调"
                        result["error_code"] = "cloudflare_challenge"
                result["error"] = result.get("error") or "没有拿到 OAuth 回调"
                result["error_code"] = result.get("error_code") or "oauth_callback_missing"
                result["debug_dir"] = _save_debug(page, profile_dir)
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
            result["error_code"] = result.get("error_code") or "browser_failed"
            result["ok"] = False
            try:
                result["debug_dir"] = _save_debug(page, profile_dir)
            except Exception:  # noqa: BLE001
                pass
        finally:
            result["phone"] = phone
            result["sms_url"] = sms_url
            release_pool_phone(phone_source)
    return result
