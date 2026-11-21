"""Chrome CDP + Playwright 注册 / 复用登录。强制走子号 ISP。"""
from __future__ import annotations

import logging
from datetime import date, datetime
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from app.core.config import load_settings
from app.integrations.openai.browser.environment import (
    BrowserEnvironmentError, context_options, environment_summary, harden_context,
)
from app.integrations.mail.otp import list_mailbox_codes, wait_for_mailbox_item
from app.integrations.proxy.socks_bridge import chrome_proxy_launch
from app.integrations.sms.client import require_proxy, sms_client

settings = load_settings()

logger = logging.getLogger(__name__)

StageCallback = Optional[Callable[[str, str], None]]

_SESSION_JS = """async () => {
  const r = await fetch('/api/auth/session', {credentials:'include', cache:'no-store'});
  const text = await r.text();
  let json = null;
  try { json = JSON.parse(text); } catch (e) {}
  return {status: r.status, json};
}"""
_PEEK_HOSTS = {"chatgpt.com", "www.chatgpt.com", "chat.openai.com", "www.chat.openai.com"}
_CF_MARKERS = (
    "just a moment",
    "checking your browser",
    "verify you are human",
    "verifying...",
    "cf-turnstile",
)
_PASSWORD_ERRORS = (
    "incorrect",
    "wrong password",
    "did not match",
    "invalid password",
    "password is required",
)


def looks_like_cloudflare(title: str, body: str = "") -> bool:
    blob = f"{title}\n{body}".lower()
    return any(marker in blob for marker in _CF_MARKERS)


def ensure_virtual_display() -> None:
    """有头 Chromium 需要 X 显示器。docker restart 会留下 stale Xvfb lock，这里自动拉起来。"""
    if settings.browser_headless or os.name == "nt" or not shutil.which("Xvfb"):
        return
    display = os.environ.get("DISPLAY") or ":99"
    os.environ["DISPLAY"] = display
    num = display.lstrip(":").split(".", 1)[0]
    sock_path = f"/tmp/.X11-unix/X{num}"
    lock_path = f"/tmp/.X{num}-lock"

    def alive() -> bool:
        if not os.path.exists(sock_path):
            return False
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            conn.settimeout(0.4)
            conn.connect(sock_path)
            return True
        except OSError:
            return False
        finally:
            conn.close()

    if alive():
        return
    for path in (sock_path, lock_path):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except IsADirectoryError:
            pass
    Path("/tmp/.X11-unix").mkdir(parents=True, exist_ok=True)
    log = open("/tmp/xvfb.log", "ab", buffering=0)
    subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1280x900x24", "-ac", "+extension", "GLX", "+render", "-noreset"],
        stdout=log,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    for _ in range(40):
        if alive():
            logger.info("Xvfb ready on %s", display)
            return
        time.sleep(0.1)
    raise RuntimeError(f"Xvfb 没起来，DISPLAY={display}")


def chromium_context_kwargs(
    profile_dir: Path | str, proxy_config: dict[str, str], *, executable_path: str = "",
) -> dict[str, Any]:
    kwargs = context_options(profile_dir, proxy_config, settings=settings, executable_path=executable_path)
    ensure_virtual_display()
    return kwargs


def session_access_token(session: dict[str, Any] | None) -> str:
    payload = session if isinstance(session, dict) else {}
    js = payload.get("json") if isinstance(payload.get("json"), dict) else {}
    return str((js or {}).get("accessToken") or (js or {}).get("access_token") or "").strip()


def session_user(session: dict[str, Any] | None) -> dict[str, Any]:
    payload = session if isinstance(session, dict) else {}
    js = payload.get("json") if isinstance(payload.get("json"), dict) else {}
    user = (js or {}).get("user") if isinstance((js or {}).get("user"), dict) else {}
    return user or {}


def can_peek_session(url: str) -> bool:
    host = (urlparse(url or "").netloc or "").lower()
    return host in _PEEK_HOSTS


def _click_first(page, selectors: list[str]) -> bool:
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=2500)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _click_exact(page, names: list[str]) -> bool:
    for name in names:
        try:
            loc = page.get_by_role("button", name=name, exact=True)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=2500)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _fill_first(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click()
                loc.first.fill("")
                loc.first.type(value, delay=30)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector)
        return loc.count() > 0 and loc.first.is_visible()
    except Exception:  # noqa: BLE001
        return False


def looks_like_otp_input(*, name: str = "", placeholder: str = "", autocomplete: str = "", input_type: str = "") -> bool:
    name_l = str(name or "").lower()
    ph = str(placeholder or "").lower()
    ac = str(autocomplete or "").lower()
    itype = str(input_type or "").lower()
    if itype == "email":
        return False
    skip_bits = ("age", "name", "birth", "email", "user", "full")
    if name_l in {"email", "name", "age", "fullname", "firstname", "lastname", "username", "birthdate", "birthday"}:
        return False
    if any(bit in name_l for bit in skip_bits) or any(bit in ph for bit in skip_bits):
        return False
    if ac in {"name", "bday", "email", "username"}:
        return False
    return True


def looks_like_about_you(*, title: str = "", body: str = "", url: str = "") -> bool:
    blob = f"{title}\n{body}\n{url}".lower()
    return any(
        token in blob
        for token in (
            "how old are you",
            "about-you",
            "finish creating account",
            "about you",
            "when were you born",
            "date of birth",
            "birthday",
        )
    )


ABOUT_YOU_STUCK_LIMIT = 4
GOTO_RETRY_LIMIT = 3
GOTO_RETRY_MARKERS = (
    "err_connection",
    "err_tunnel",
    "err_socks",
    "err_proxy",
    "err_timed_out",
    "err_empty_response",
    "err_connection_reset",
    "err_connection_refused",
    "err_connection_closed",
    "net::err_",
    "timeout",
    "connection closed",
)


def should_retry_goto(error: str) -> bool:
    text = str(error or "").lower()
    return any(token in text for token in GOTO_RETRY_MARKERS)


def goto_with_retries(page, url: str, *, report: StageCallback = None, attempts: int = GOTO_RETRY_LIMIT):
    last_error = ""
    for index in range(max(1, int(attempts))):
        try:
            page.goto(url, wait_until="load")
            return True
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            if index + 1 >= attempts or not should_retry_goto(last_error):
                raise
            if report:
                report("browser_open", f"打开页面失败，重试第 {index + 1} 次")
            page.wait_for_timeout(2500)
    if last_error:
        raise RuntimeError(last_error)
    return False


def about_you_birth_year(now=None) -> str:
    year = getattr(now, "year", None)
    if year is None:
        year = datetime.now().year
    return str(int(year) - 28)

EMAIL_INPUT_SELECTOR = (
    'input#email, input[name="email"], input[type="email"], '
    'input[autocomplete="email"], input[name="username"], input[autocomplete="username"], '
    'input[placeholder="Email address"], input[aria-label="Email address"]'
)
REGISTER_START_URL = "https://chatgpt.com/auth/login"
LOGIN_START_URL = "https://chatgpt.com/auth/login"


def looks_like_email_gate(*, title: str = "", body: str = "", url: str = "") -> bool:
    title_l = str(title or "").lower()
    body_l = str(body or "").lower()
    url_l = str(url or "").lower()
    blob = f"{title_l}\n{body_l}\n{url_l}"
    if any(token in blob for token in (
        "email-verification",
        "check your inbox",
        "enter your password",
        "how old are you",
        "about-you",
        "log-in/password",
    )):
        return False
    if "log in or sign up" in blob:
        return True
    if "get started" in title_l and "chatgpt" in title_l:
        return True
    if "/auth/login" in url_l:
        return True
    return False


REGISTRATION_TEXT_MARKERS = ("create a password", "create your account")
NOT_REGISTERED_ERROR = "这个邮箱还没有注册 ChatGPT，号池只接收已注册账号"


def looks_like_registration(*, url: str = "", body: str = "") -> bool:
    """Login-only mode: OpenAI moved this email into account creation."""
    url_l = str(url or "").lower()
    if "create-account" in url_l and "log-in-or-create-account" not in url_l:
        return True
    if "auth.openai.com" not in url_l or "/log-in" in url_l:
        return False
    blob = str(body or "").lower()
    return any(token in blob for token in REGISTRATION_TEXT_MARKERS)


def looks_like_session_ended(*, title: str = "", body: str = "") -> bool:
    blob = f"{title}\n{body}".lower()
    return "session has ended" in blob


def split_phone(phone: str) -> tuple[str, str]:
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if digits.startswith("86") and len(digits) >= 12:
        return "China", digits[2:]
    if digits.startswith("86"):
        return "China", digits[2:]
    if digits.startswith("1") and len(digits) == 11:
        return "United States", digits[1:]
    if len(digits) == 10:
        return "United States", digits
    return "", digits


def looks_like_invalid_phone(text: str) -> bool:
    blob = (text or "").lower()
    return any(token in blob for token in (
        "phone number is not valid",
        "is not valid",
        "not a valid phone",
        "invalid phone number",
        "already linked",
        "maximum number of accounts",
        "too many accounts",
    ))


def phone_rejection_message(text: str) -> str:
    blob = (text or "").lower()
    if "already linked" in blob or "maximum number of accounts" in blob:
        return "这个接码号已经绑满 OpenAI 账号，换一个没用过的 +1 号"
    if looks_like_invalid_phone(text):
        return "手机号不被 OpenAI 接受。Codex 授权通常不吃 +86，要换能过的接码号"
    return ""


def looks_like_recently_used_phone(text: str) -> bool:
    blob = (text or "").lower()
    return any(token in blob for token in (
        "recently used",
        "used recently",
        "try a different number",
        "use a different number",
        "use a different phone",
        "please use a different",
        "请使用其他",
        "换一个号码",
    ))


def looks_like_phone_risk(text: str) -> bool:
    blob = (text or "").lower()
    return any(token in blob for token in (
        "couldn't send a text",
        "could not send a text",
        "can't send a text",
        "cannot send a text",
        "unable to send",
        "we couldn't send",
        "continue with whatsapp",
        "try whatsapp",
        "sent a code to whatsapp",
        "check whatsapp",
        "verify with whatsapp",
        "无法向该号发短信",
        "无法发送短信",
    ))


def classify_phone_outcome(*, text: str = "", error: str = "", error_code: str = "") -> str:
    blob = f"{text}\n{error}\n{error_code}".lower()
    code = (error_code or "").lower()
    if code == "cancelled" or "已取消" in (error or ""):
        return "cancelled"
    if code in {"cloudflare_challenge", "proxy_failed", "email_gate_stuck"} or "cloudflare" in blob:
        return "unrelated"
    if looks_like_recently_used_phone(blob) or "请使用其他号码" in (error or ""):
        return "recently_used"
    if looks_like_phone_risk(blob):
        return "risk"
    if code == "sms_rejected" or looks_like_invalid_phone(text or error):
        return "invalid"
    if "sms otp timeout" in blob or "接码超时" in (error or "") or "timeout after" in blob:
        return "no_sms"
    if code == "sms_failed" and (
        "timeout" in blob or "空号" in (error or "") or "没能发出短信" in (error or "")
    ):
        return "no_sms"
    return "unrelated"


def phone_outcome_message(outcome: str, text: str = "") -> str:
    if outcome == "recently_used":
        return "这个号码刚被用过，OpenAI 要求换号"
    if outcome == "risk":
        return "无法向该号发短信 / 被切到 WhatsApp"
    if outcome == "invalid":
        return phone_rejection_message(text) or "手机号不被 OpenAI 接受"
    if outcome == "no_sms":
        return "接码超时或空号"
    return phone_rejection_message(text)


def phone_page_outcome(page, *, allow_risk: bool = True) -> tuple[str, str]:
    blob = _page_text(page)
    outcome = classify_phone_outcome(text=blob, error=phone_rejection_message(blob))
    if outcome == "risk" and not allow_risk:
        outcome = "unrelated"
    if outcome not in {"", "unrelated"}:
        return outcome, phone_outcome_message(outcome, blob)
    rejected = page_phone_rejection(page)
    if rejected:
        classified = classify_phone_outcome(text=rejected, error=rejected, error_code="sms_rejected")
        if classified == "risk" and not allow_risk:
            classified = "unrelated"
        if classified not in {"", "unrelated"}:
            return classified, rejected
    return "", ""


def take_pool_phone(phone_source, report) -> tuple[str, str, str, str]:
    if not phone_source:
        return "", "", "需要接码，但未提供手机号", "sms_missing"
    try:
        got = phone_source("acquire") or {}
    except Exception as exc:  # noqa: BLE001
        return "", "", str(exc), "phone_pool_empty"
    if got.get("ok") and got.get("number") and str(got.get("sms_url") or "").startswith("http"):
        number = str(got["number"])
        report("add_phone", f"领取 {number}")
        return number, str(got["sms_url"]), "", ""
    return "", "", str(got.get("error") or "号码池暂时没有可领的号"), str(got.get("error_code") or "phone_pool_empty")


def record_pool_phone(phone_source, result: str, message: str = "") -> None:
    if not phone_source:
        return
    try:
        phone_source("record", result=result, message=message)
    except Exception:  # noqa: BLE001
        logger.debug("phone_source record failed", exc_info=True)


def release_pool_phone(phone_source) -> None:
    if not phone_source:
        return
    try:
        phone_source("release")
    except Exception:  # noqa: BLE001
        logger.debug("phone_source release failed", exc_info=True)


def page_phone_rejection(page) -> str:
    blob = _page_text(page)
    msg = phone_rejection_message(blob)
    if msg:
        return msg
    for needle, sample in (
        ("already linked", "already linked to the maximum number of accounts"),
        ("maximum number of accounts", "already linked to the maximum number of accounts"),
        ("Phone number is not valid", "phone number is not valid"),
    ):
        try:
            loc = page.get_by_text(needle, exact=False)
            if loc.count() > 0 and loc.first.is_visible():
                return phone_rejection_message(sample)
        except Exception:  # noqa: BLE001
            continue
    return ""


def _phone_country_label(page) -> str:
    for name in ("China (+86)", "China", "United States (+1)", "United States"):
        try:
            loc = page.get_by_role("button", name=name)
            if loc.count() > 0 and loc.first.is_visible():
                return name
        except Exception:  # noqa: BLE001
            continue
    return ""


def _select_phone_country(page, country: str) -> bool:
    current = _phone_country_label(page)
    wanted = (country or "").lower()
    if wanted == "china" and current.lower().startswith("china"):
        return True
    if wanted == "united states" and current.lower().startswith("united"):
        return True
    opened = False
    for name in (current, "United States (+1)", "United States", "+1"):
        if not name:
            continue
        try:
            loc = page.get_by_role("button", name=name)
            if loc.count() > 0 and loc.first.is_visible():
                loc.first.click(timeout=2500)
                opened = True
                break
        except Exception:  # noqa: BLE001
            continue
    if not opened:
        return False
    page.wait_for_timeout(400)
    try:
        search = page.locator('input[type="search"], input[placeholder*="Search" i]')
        if search.count() > 0 and search.first.is_visible():
            search.first.fill(country)
            page.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        pass
    labels = ["China (+86)", "China (Mainland)", "Mainland China", "China"] if wanted == "china" else [country]
    for label in labels:
        try:
            page.get_by_text(label, exact=False).first.click(timeout=2500)
            page.wait_for_timeout(400)
            current = _phone_country_label(page)
            if wanted == "china" and "china" in current.lower():
                return True
            if wanted and wanted in current.lower():
                return True
        except Exception:  # noqa: BLE001
            continue
    current = _phone_country_label(page)
    if wanted == "china":
        return "china" in current.lower()
    return bool(wanted) and wanted in current.lower()


def _fill_phone_number(page, phone: str) -> bool:
    country, national = split_phone(phone)
    if not national:
        return False
    if country == "China":
        if not _select_phone_country(page, country):
            return False
    elif country:
        _select_phone_country(page, country)
    return _fill_first(page, ['input[type="tel"]', 'input[name="phone"]'], national)


def _choose_sms_channel(page) -> bool:
    if _click_first(page, [
        'button:has-text("Text")',
        'button:has-text("SMS")',
        'button:has-text("Text Message")',
        'label:has-text("Text")',
        'label:has-text("SMS")',
        'label:has-text("Text Message")',
    ]):
        return True
    return _click_exact(page, ["Text", "SMS", "Text Message"])


def _submit_phone_sms(page) -> None:
    _choose_sms_channel(page)
    page.wait_for_timeout(400)
    _click_first(page, [
        'button[type="submit"]',
        'button:has-text("Send")',
        'button:has-text("Continue")',
    ])


def _page_past_otp(page) -> bool:
    url = (getattr(page, "url", "") or "").lower()
    title = ""
    try:
        title = page.title() or ""
    except Exception:  # noqa: BLE001
        title = ""
    body = _page_text(page)
    if looks_like_about_you(title=title, body=body, url=url) or "about-you" in url:
        return True
    if _visible(page, 'input[name="age"], input[placeholder*="Age" i]'):
        return True
    return bool(session_access_token(_peek_session(page)))


def _email_input_visible(page) -> bool:
    return _visible(page, EMAIL_INPUT_SELECTOR)


def _current_email_value(page) -> str:
    try:
        loc = page.locator(EMAIL_INPUT_SELECTOR)
        if loc.count() == 0:
            return ""
        return (loc.first.input_value() or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _fill_email(page, email: str) -> bool:
    selectors = [
        'input#email',
        'input[name="email"]',
        'input[type="email"]',
        'input[autocomplete="email"]',
        'input[name="username"]',
        'input[autocomplete="username"]',
        'input[placeholder="Email address"]',
        'input[aria-label="Email address"]',
    ]
    if _fill_first(page, selectors, email) and _current_email_value(page).lower() == email.lower():
        return True
    try:
        ok = page.evaluate(
            """([sel, val]) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                el.focus();
                const proto = window.HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                desc.set.call(el, val);
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
                return el.value === val;
            }""",
            [EMAIL_INPUT_SELECTOR, email],
        )
        return bool(ok) or _current_email_value(page).lower() == email.lower()
    except Exception:  # noqa: BLE001
        return _current_email_value(page).lower() == email.lower()


def _find_otp(page):
    for selector in [
        'input[autocomplete="one-time-code"]',
        'input[name="code"]',
        'input[name="otp"]',
        'input[name="pin"]',
        'input[placeholder="Code"]',
        'input[placeholder*="Code" i]',
        'input[aria-label*="code" i]',
        'input[aria-label*="verification" i]',
    ]:
        loc = page.locator(selector)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                el = loc.first
                if looks_like_otp_input(
                    name=el.get_attribute("name") or "",
                    placeholder=el.get_attribute("placeholder") or "",
                    autocomplete=el.get_attribute("autocomplete") or "",
                    input_type=el.get_attribute("type") or "",
                ):
                    return el
        except Exception:  # noqa: BLE001
            continue
    return None


def _otp_boxes(page):
    try:
        loc = page.locator("input[maxlength='1']")
        if loc.count() >= 4 and loc.first.is_visible():
            return loc
    except Exception:  # noqa: BLE001
        pass
    return None


def _fill_otp(page, code: str) -> bool:
    boxes = _otp_boxes(page)
    if boxes is not None:
        for index, char in enumerate(code[: boxes.count()]):
            boxes.nth(index).fill(char)
        return True
    otp_el = _find_otp(page)
    if otp_el:
        otp_el.fill(code)
        return True
    return False


def _button_clickable(loc) -> bool:
    try:
        if loc.count() == 0:
            return False
        btn = loc.first
        if not btn.is_visible() or not btn.is_enabled():
            return False
        if (btn.get_attribute("aria-disabled") or "").strip().lower() == "true":
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def _wait_and_fallback_submit_otp(
    page,
    *,
    started_url: str,
    exact_names: list[str] | None = None,
    selectors: list[str] | None = None,
    wait_sec: float = 3.0,
) -> bool:
    """Wait up to wait_sec for OTP auto-submit; click submit once only if still on the OTP page."""
    base_url = (started_url or "").split("#", 1)[0]
    deadline = time.monotonic() + max(0.0, float(wait_sec))
    while time.monotonic() < deadline:
        page.wait_for_timeout(250)
        current_url = (page.url or "").split("#", 1)[0]
        if current_url and base_url and current_url != base_url:
            return False
        if not _find_otp(page) and not _otp_boxes(page):
            return False
    current_url = (page.url or "").split("#", 1)[0]
    if current_url and base_url and current_url != base_url:
        return False
    if not _find_otp(page) and not _otp_boxes(page):
        return False
    for name in (exact_names or []):
        try:
            loc = page.get_by_role("button", name=name, exact=True)
            if _button_clickable(loc):
                loc.first.click(timeout=2500)
                return True
        except Exception:  # noqa: BLE001
            continue
    for selector in (selectors or []):
        try:
            loc = page.locator(selector)
            if _button_clickable(loc):
                loc.first.click(timeout=2500)
                return True
        except Exception:  # noqa: BLE001
            continue
    return False


def page_rate_limited(page) -> bool:
    blob = _page_text(page).lower()
    return any(token in blob for token in ("too many attempts", "too many tries", "max_check_attempts"))


def _page_text(page) -> str:
    try:
        return f"{page.title() or ''}\n{page.locator('body').inner_text(timeout=800) or ''}"
    except Exception:  # noqa: BLE001
        try:
            return page.title() or ""
        except Exception:  # noqa: BLE001
            return ""


def page_is_cloudflare(page) -> bool:
    return looks_like_cloudflare(_page_text(page))


def wait_cloudflare(page, timeout_sec: int = 45) -> bool:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if not page_is_cloudflare(page):
            return True
        page.wait_for_timeout(1000)
    return not page_is_cloudflare(page)


def _peek_session(page) -> dict[str, Any]:
    try:
        if not can_peek_session(page.url or ""):
            return {}
        return page.evaluate(_SESSION_JS) or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _poll_session(page) -> dict[str, Any]:
    try:
        resp = page.request.get("https://chatgpt.com/api/auth/session")
        data = resp.json() if resp.text() else {}
        return {"status": resp.status, "json": data if isinstance(data, dict) else {}}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


def _extract_session(page) -> dict[str, Any]:
    polled = _poll_session(page)
    if session_access_token(polled):
        return polled
    peeked = _peek_session(page)
    if session_access_token(peeked):
        return peeked
    title = ""
    try:
        title = page.title() or ""
    except Exception:  # noqa: BLE001
        title = ""
    if _email_input_visible(page) or looks_like_email_gate(title=title, url=getattr(page, "url", "") or ""):
        return polled if polled.get("status") else peeked or polled
    try:
        page.goto("https://chatgpt.com/", wait_until="load", timeout=30000)
        wait_cloudflare(page, timeout_sec=30)
        page.wait_for_timeout(1200)
    except Exception:  # noqa: BLE001
        pass
    peeked = _peek_session(page)
    if session_access_token(peeked):
        return peeked
    return polled if polled.get("status") else peeked or polled


def _session_failure(page, session: dict[str, Any]) -> str:
    title = ""
    try:
        title = page.title() or ""
    except Exception:  # noqa: BLE001
        pass
    prefix = "cloudflare challenge; " if looks_like_cloudflare(title, _page_text(page)) else ""
    return (
        f"{prefix}no accessToken in session; "
        f"url={getattr(page, 'url', '') or ''}; "
        f"title={title}; "
        f"status={session.get('status')}"
    )


def _save_debug(page, profile_dir: Path) -> str:
    resolved = Path(profile_dir).resolve()
    base = resolved.parent.parent.parent if resolved.parent.name == "chromix" else resolved.parent.parent
    dest = base / "debug" / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(dest / "page.png"), full_page=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        (dest / "page.html").write_text(page.content(), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    try:
        (dest / "meta.txt").write_text(f"{page.url}\n{page.title()}\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    return str(dest)


def _mail_kwargs(
    *,
    email: str,
    pickup_url: str,
    proxy: str,
    use_cloudflare: bool,
    cf_base_url: str,
    cf_address: str,
    cf_admin_password: str,
    hme_base_url: str = "",
    hme_service_token: str = "",
    hme_account_id: str = "",
) -> dict[str, str]:
    return {
        "email": email,
        "pickup_url": pickup_url,
        "proxy": proxy,
        "cf_base_url": cf_base_url if use_cloudflare else "",
        "cf_address": cf_address if use_cloudflare else "",
        "cf_admin_password": cf_admin_password if use_cloudflare else "",
        "hme_base_url": hme_base_url,
        "hme_service_token": hme_service_token,
        "hme_account_id": hme_account_id,
    }


def _snapshot_mailbox_codes(**kwargs: str) -> set[str]:
    if not (kwargs.get("pickup_url") or kwargs.get("cf_admin_password") or kwargs.get("hme_service_token")):
        return set()
    try:
        return set(list_mailbox_codes(**kwargs))
    except Exception:  # noqa: BLE001
        logger.warning("快照邮箱验证码失败", exc_info=True)
        return set()


OTP_WAIT_SEC = 60
OTP_RESEND_MAX = 2


def _wait_mailbox_code(
    *,
    email: str,
    pickup_url: str,
    proxy: str,
    use_cloudflare: bool,
    cf_base_url: str,
    cf_address: str,
    cf_admin_password: str,
    hme_base_url: str = "",
    hme_service_token: str = "",
    hme_account_id: str = "",
    ignore: Optional[set[str]] = None,
    timeout_sec: float = OTP_WAIT_SEC,
) -> str:
    if not (pickup_url or use_cloudflare or hme_service_token):
        return ""
    return (
        wait_for_mailbox_item(
            email=email,
            pickup_url=pickup_url,
            proxy=proxy,
            kind="code",
            timeout_sec=timeout_sec,
            cf_base_url=cf_base_url if use_cloudflare else "",
            cf_address=cf_address if use_cloudflare else "",
            cf_admin_password=cf_admin_password if use_cloudflare else "",
            hme_base_url=hme_base_url,
            hme_service_token=hme_service_token,
            hme_account_id=hme_account_id,
            ignore_values=ignore,
        )
        or ""
    )


def _click_resend_email(page) -> bool:
    return _click_exact(page, ["Resend email", "Resend code", "Resend"]) or _click_first(
        page,
        [
            'button[value="resend"]',
            'button[name="intent"][value="resend"]',
            'button:has-text("Resend email")',
            'button:has-text("Resend code")',
        ],
    )


def _resend_email_otp(
    page,
    *,
    resends: int,
    report: StageCallback = None,
    known_codes: Optional[set[str]] = None,
    mail_kw: Optional[dict[str, str]] = None,
) -> tuple[int, bool]:
    if resends >= OTP_RESEND_MAX:
        return resends, False
    if known_codes is not None and mail_kw:
        known_codes.update(_snapshot_mailbox_codes(**mail_kw))
    if not _click_resend_email(page):
        return resends, False
    resends += 1
    if report:
        report("email_otp", f"{OTP_WAIT_SEC}s 未读到验证码，已重新发送（第 {resends}/{OTP_RESEND_MAX} 次）")
    try:
        page.wait_for_timeout(2000)
    except Exception:  # noqa: BLE001
        pass
    return resends, True


def wait_email_otp_with_resend(
    page,
    *,
    email: str,
    pickup_url: str,
    proxy: str,
    use_cloudflare: bool,
    cf_base_url: str,
    cf_address: str,
    cf_admin_password: str,
    hme_base_url: str = "",
    hme_service_token: str = "",
    hme_account_id: str = "",
    ignore: Optional[set[str]] = None,
    resends: int = 0,
    report: StageCallback = None,
    check_past_otp: bool = True,
) -> tuple[str, int, str]:
    known = ignore if ignore is not None else set()
    mail_kw = _mail_kwargs(
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
    try:
        code = _wait_mailbox_code(
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
            ignore=known,
            timeout_sec=OTP_WAIT_SEC,
        )
    except Exception as exc:  # noqa: BLE001
        if check_past_otp and _page_past_otp(page):
            if report:
                report("email_otp", "没读到验证码，但页面已经过了验证")
            return "", resends, ""
        resends, resent = _resend_email_otp(
            page, resends=resends, report=report, known_codes=known, mail_kw=mail_kw
        )
        if resent:
            return "", resends, ""
        return "", resends, f"email OTP failed: {exc}"
    if code:
        return code, resends, ""
    if check_past_otp and _page_past_otp(page):
        if report:
            report("email_otp", "没读到验证码，但页面已经过了验证")
        return "", resends, ""
    if not (pickup_url or use_cloudflare or hme_service_token):
        return "", resends, "email OTP not found"
    resends, resent = _resend_email_otp(
        page, resends=resends, report=report, known_codes=known, mail_kw=mail_kw
    )
    if resent:
        return "", resends, ""
    return "", resends, "email OTP not found"


def _set_input(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() == 0 or not loc.first.is_visible():
                continue
            loc.first.click(timeout=2500)
            loc.first.fill(value)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _option_labels(locator) -> list[str]:
    labels: list[str] = []
    try:
        count = locator.count()
    except Exception:  # noqa: BLE001
        return labels
    for index in range(count):
        try:
            text = str(locator.nth(index).inner_text(timeout=800) or "").strip()
        except Exception:  # noqa: BLE001
            text = ""
        if text:
            labels.append(text)
    return labels


def pick_select_option(labels: list[str], wanted: str) -> str:
    target = str(wanted or "").strip().lower()
    cleaned = [str(item or "").strip() for item in labels if str(item or "").strip()]
    if not target or not cleaned:
        return ""
    for item in cleaned:
        if item.lower() == target:
            return item
    for item in cleaned:
        if target in item.lower() or item.lower() in target:
            return item
    return ""


def _select_named_option(page, names: list[str], wanted: str) -> bool:
    label = pick_select_option(_collect_select_labels(page, names), wanted)
    if not label:
        return False
    return _choose_select_label(page, names, label)


def _collect_select_labels(page, names: list[str]) -> list[str]:
    for name in names:
        loc = _first_visible_select(page, name)
        if loc is None:
            continue
        labels = _option_labels(loc.locator("option"))
        if labels:
            return labels
    return []


def _first_visible_select(page, name: str):
    selectors = [
        f'select[name="{name}"]',
        f'select[id="{name}"]',
        f'select[autocomplete="{name}"]',
        f'select[aria-label*="{name}" i]',
    ]
    if name == "bday-month":
        selectors.extend(['select[name="month"]', 'select[autocomplete="bday-month"]', 'select[aria-label*="Month" i]'])
    elif name == "bday-day":
        selectors.extend(['select[name="day"]', 'select[autocomplete="bday-day"]', 'select[aria-label*="Day" i]'])
    elif name == "bday-year":
        selectors.extend(['select[name="year"]', 'select[autocomplete="bday-year"]', 'select[aria-label*="Year" i]'])
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() == 0 or not loc.first.is_visible():
                continue
            return loc.first
        except Exception:  # noqa: BLE001
            continue
    return None


def _choose_select_label(page, names: list[str], label: str) -> bool:
    for name in names:
        loc = _first_visible_select(page, name)
        if loc is None:
            continue
        try:
            loc.select_option(label=label, timeout=2500)
            return True
        except Exception:  # noqa: BLE001
            try:
                loc.select_option(value=label, timeout=2500)
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


_FIRST_NAMES = (
    "James", "Oliver", "Henry", "Daniel", "Lucas", "Ethan",
    "Emma", "Amelia", "Grace", "Sophie", "Chloe", "Hannah",
)
_LAST_NAMES = (
    "Smith", "Taylor", "Wilson", "Clark", "Walker", "Green",
    "Carter", "Bennett", "Morgan", "Cooper", "Reed", "Parker",
)
_MONTH_NAMES = (
    ("January", "Jan"), ("February", "Feb"), ("March", "Mar"), ("April", "Apr"),
    ("May", "May"), ("June", "Jun"), ("July", "Jul"), ("August", "Aug"),
    ("September", "Sep"), ("October", "Oct"), ("November", "Nov"), ("December", "Dec"),
)


def _random_about_you_profile() -> dict[str, Any]:
    today = date.today()
    age = 22 + secrets.randbelow(24)
    month = 1 + secrets.randbelow(12)
    day = 1 + secrets.randbelow(28)
    year = today.year - age - ((month, day) > (today.month, today.day))
    return {
        "name": f"{secrets.choice(_FIRST_NAMES)} {secrets.choice(_LAST_NAMES)}",
        "age": age,
        "year": year,
        "month": month,
        "day": day,
        "birthday": f"{year:04d}-{month:02d}-{day:02d}",
    }


def _fill_birthday_selects(
    page,
    *,
    month: int = 1,
    day: int = 15,
    year: int | str | None = None,
) -> bool:
    year_str = str(year) if year is not None else about_you_birth_year()
    month_idx = max(1, min(12, int(month)))
    full_month, abbr_month = _MONTH_NAMES[month_idx - 1]
    month_ok = (
        _select_named_option(page, ["bday-month", "month"], full_month)
        or _select_named_option(page, ["bday-month", "month"], abbr_month)
        or _select_named_option(page, ["bday-month", "month"], f"{month_idx:02d}")
        or _select_named_option(page, ["bday-month", "month"], str(month_idx))
    )
    day_int = max(1, min(28, int(day)))
    day_ok = (
        _select_named_option(page, ["bday-day", "day"], str(day_int))
        or _select_named_option(page, ["bday-day", "day"], f"{day_int:02d}")
    )
    year_ok = _select_named_option(page, ["bday-year", "year"], year_str)
    return bool(year_ok and month_ok and day_ok)


def _fill_spinbutton(page, selectors: list[str], value: str) -> bool:
    for selector in selectors:
        try:
            loc = page.locator(selector)
            if loc.count() == 0 or not loc.first.is_visible():
                continue
            loc.first.click(timeout=2500)
            page.keyboard.type(value, delay=40)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


def _fill_birthday_inputs(page, *, month: int, day: int, year: int, birthday: str) -> bool:
    if _set_input(page, ['input[type="date"]'], birthday):
        return True
    month_ok = _set_input(
        page,
        ['input[name="month"]', 'input[autocomplete="bday-month"]', 'input[aria-label*="Month" i]', 'input[placeholder*="MM" i]'],
        f"{month:02d}",
    )
    day_ok = _set_input(
        page,
        ['input[name="day"]', 'input[autocomplete="bday-day"]', 'input[aria-label*="Day" i]', 'input[placeholder*="DD" i]'],
        f"{day:02d}",
    )
    year_ok = _set_input(
        page,
        ['input[name="year"]', 'input[autocomplete="bday-year"]', 'input[aria-label*="Year" i]', 'input[placeholder*="YYYY" i]'],
        f"{year:04d}",
    )
    if month_ok and day_ok and year_ok:
        return True
    spin_month = _fill_spinbutton(
        page,
        ['[role="spinbutton"][data-type="month"]', '[role="spinbutton"][aria-label*="month" i]'],
        f"{month:02d}",
    )
    spin_day = _fill_spinbutton(
        page,
        ['[role="spinbutton"][data-type="day"]', '[role="spinbutton"][aria-label*="day" i]'],
        f"{day:02d}",
    )
    spin_year = _fill_spinbutton(
        page,
        ['[role="spinbutton"][data-type="year"]', '[role="spinbutton"][aria-label*="year" i]'],
        f"{year:04d}",
    )
    if spin_month and spin_day and spin_year:
        return True
    return _set_input(
        page,
        ['input[autocomplete="bday"]', 'input[name="birthdate"]', 'input[name="birthday"]'],
        f"{month:02d}/{day:02d}/{year:04d}",
    )


def _wait_about_you_leave(page, started_url: str) -> bool:
    start = (started_url or "").split("#", 1)[0]
    for _ in range(16):
        page.wait_for_timeout(500)
        current = (page.url or "").split("#", 1)[0]
        if current and current != start:
            return True
        if not looks_like_about_you(title=page.title() or "", body=_page_text(page), url=current):
            return True
    return False


def _fill_about_you(page) -> bool:
    started_url = page.url or ""
    profile = _random_about_you_profile()
    name_ok = _set_input(
        page,
        [
            'input[name="name"]',
            'input[name="fullName"]',
            'input[autocomplete="name"]',
            'input[placeholder="Full name"]',
            'input[placeholder*="Full name" i]',
        ],
        profile["name"],
    )
    birthday_ok = _fill_birthday_selects(
        page,
        month=profile["month"],
        day=profile["day"],
        year=profile["year"],
    )
    if not birthday_ok:
        birthday_ok = _fill_birthday_inputs(
            page,
            month=profile["month"],
            day=profile["day"],
            year=profile["year"],
            birthday=profile["birthday"],
        )
    age_ok = _set_input(
        page,
        [
            'input[name="age"]',
            'input[placeholder="Age"]',
            'input[placeholder*="Age" i]',
        ],
        str(profile["age"]),
    )
    if not birthday_ok and not age_ok:
        age_ok = _set_input(page, ['input[type="number"]'], str(profile["age"]))
    filled = bool(name_ok or age_ok or birthday_ok)
    if not filled:
        return False
    clicked = (
        _click_exact(page, ["Finish creating account", "Continue", "Agree", "完成注册", "继续"])
        or _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Finish")'])
    )
    if not clicked:
        return False
    _wait_about_you_leave(page, started_url)
    return True


def _pick_workspace(page, team_name: str = "") -> bool:
    body = _page_text(page).lower()
    url = (page.url or "").lower()
    if not any(token in f"{body}\n{url}" for token in ("workspace", "工作空间", "organization", "accept-invite", "consent")):
        return False
    want = str(team_name or "").strip()
    if want and _click_exact(page, [want]):
        _click_exact(page, ["Continue", "Join", "Confirm", "Accept"])
        return True
    return _click_exact(page, ["Accept invite", "Join workspace", "Join", "Accept"])

def _accept_terms(page) -> bool:
    if not _visible(page, "input[type='checkbox']"):
        return False
    try:
        box = page.locator("input[type='checkbox']").first
        if not box.is_checked():
            box.check(timeout=2500)
    except Exception:  # noqa: BLE001
        _click_first(page, ["input[type='checkbox']"])
    return _click_exact(page, ["Continue", "I agree", "Accept"]) or _click_first(page, ['button[type="submit"]'])


def run_browser_onboard(
    *,
    email: str,
    password: str,
    pickup_url: str = "",
    phone: str = "",
    sms_url: str = "",
    proxy: str,
    start_url: str = "",
    mode: str = "register",
    team_name: str = "",
    use_cloudflare: bool = False,
    cf_base_url: str = "",
    cf_address: str = "",
    cf_admin_password: str = "",
    on_stage: StageCallback = None,
    phone_source=None,
    allow_sms: bool = True,
    executable_path: str = "",
    invite_entry: bool = False,
    continue_in_browser=None,
    signup_flow: str | None = None,
    hme_base_url: str = "",
    hme_service_token: str = "",
    hme_account_id: str = "",
) -> Dict[str, Any]:
    """mode: register / relogin / login (existing account only; stops on any account-creation page)."""
    require_proxy(proxy, "子号浏览器")
    if not allow_sms:
        phone, sms_url, phone_source = "", "", None
    if invite_entry:
        from app.integrations.mail.otp import extract_invite_url
        if not start_url or extract_invite_url(start_url) != start_url:
            return {"ok": False, "error_code": "invite_link_missing", "error": "有效邀请链接缺失"}
    from playwright.sync_api import sync_playwright

    def report(stage: str, message: str) -> None:
        if on_stage:
            try:
                on_stage(stage, message)
            except Exception:  # noqa: BLE001
                logger.debug("on_stage failed", exc_info=True)

    profile_dir = Path(__file__).resolve().parents[4] / "data" / "chrome-profiles" / email.replace("@", "_at_")
    profile_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ok": False, "email": email, "password": password, "mode": mode, "phone": phone, "sms_url": sms_url}
    with chrome_proxy_launch(proxy) as proxy_config, sync_playwright() as playwright:
        try:
            launch_kwargs = chromium_context_kwargs(profile_dir, proxy_config, executable_path=executable_path)
        except BrowserEnvironmentError as exc:
            return {**result, "error_code": exc.error_code, "error": str(exc)}
        profile_dir = Path(launch_kwargs.get("user_data_dir", profile_dir))
        report("browser_environment", environment_summary(launch_kwargs))
        browser = playwright.chromium.launch_persistent_context(**launch_kwargs)
        harden_context(browser, settings=settings)
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.set_default_timeout(60000)
        try:
            target = start_url or (REGISTER_START_URL if mode == "register" else LOGIN_START_URL)
            if (signup_flow or settings.browser_signup_flow) == "extension" and mode == "register":
                target = start_url or "https://chatgpt.com/"
                from app.integrations.openai.browser.signup import run_managed_signup

                # Exactly one UI driver; a failed new runner never retries via legacy.
                phone, sms_url = "", ""
                outcome = run_managed_signup(
                    browser=browser, page=page, email=email, password=password,
                    profile_dir=profile_dir, start_url=target, invite_entry=invite_entry,
                    team_name=team_name, report=report,
                    mail_kwargs=_mail_kwargs(email=email, pickup_url=pickup_url, proxy=proxy,
                        use_cloudflare=use_cloudflare, cf_base_url=cf_base_url,
                        cf_address=cf_address, cf_admin_password=cf_admin_password,
                        hme_base_url=hme_base_url, hme_service_token=hme_service_token,
                        hme_account_id=hme_account_id),
                )
                result.update(outcome)
                if result.get("ok") and continue_in_browser is not None:
                    continue_in_browser(result, browser, page)
                return result
            from app.integrations.openai.browser.chatgpt_login import run_chatgpt_login_loop

            outcome = run_chatgpt_login_loop(
                page,
                email=email,
                password=password,
                pickup_url=pickup_url,
                phone=phone,
                sms_url=sms_url,
                proxy=proxy,
                start_url=target,
                mode=mode,
                team_name=team_name,
                use_cloudflare=use_cloudflare,
                cf_base_url=cf_base_url,
                cf_address=cf_address,
                cf_admin_password=cf_admin_password,
                hme_base_url=hme_base_url,
                hme_service_token=hme_service_token,
                hme_account_id=hme_account_id,
                profile_dir=profile_dir,
                phone_source=phone_source,
                allow_sms=allow_sms,
                allow_profile_setup=(mode != "login"),
                invite_entry=invite_entry,
                require_session_match=False,
                initial_goto=True,
                report=report,
            )
            outcome.pop("sms_code_submissions", None)
            phone = str(outcome.get("phone", phone) or "")
            sms_url = str(outcome.get("sms_url", sms_url) or "")
            result.update(outcome)
            if result.get("ok") and continue_in_browser is not None:
                continue_in_browser(result, browser, page)
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
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return result
