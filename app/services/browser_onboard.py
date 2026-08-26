"""Chrome CDP + Playwright 注册 / 复用登录。强制走子号 ISP。"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

from app.config import settings
from app.services.mail_otp import wait_for_mailbox_item
from app.services.sms import require_proxy, sms_client
from app.services.socks_bridge import chrome_proxy_launch

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


def chromium_context_kwargs(profile_dir: Path | str, proxy_config: dict[str, str]) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "user_data_dir": str(profile_dir),
        "headless": bool(settings.browser_headless),
        "proxy": proxy_config,
        "locale": "en-US",
        "viewport": {"width": 1280, "height": 900},
        "ignore_default_args": ["--enable-automation"],
        "args": [
            "--disable-features=Translate",
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-blink-features=AutomationControlled",
        ],
    }
    if settings.browser_channel:
        kwargs["channel"] = settings.browser_channel
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


def _find_otp(page):
    for selector in [
        'input[autocomplete="one-time-code"]',
        'input[inputmode="numeric"]',
        'input[name="code"]',
        'input[name="otp"]',
        'input[placeholder*="Code" i]',
        'input[aria-label*="code" i]',
        'input[aria-label*="verification" i]',
    ]:
        loc = page.locator(selector)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
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
    dest = Path(profile_dir).resolve().parent.parent / "debug" / time.strftime("%Y%m%d-%H%M%S")
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


def _wait_mailbox_code(
    *,
    email: str,
    pickup_url: str,
    proxy: str,
    use_cloudflare: bool,
    cf_base_url: str,
    cf_address: str,
    cf_admin_password: str,
) -> str:
    if not (pickup_url or use_cloudflare):
        return ""
    return (
        wait_for_mailbox_item(
            email=email,
            pickup_url=pickup_url,
            proxy=proxy,
            kind="code",
            timeout_sec=90,
            cf_base_url=cf_base_url if use_cloudflare else "",
            cf_address=cf_address if use_cloudflare else "",
            cf_admin_password=cf_admin_password if use_cloudflare else "",
        )
        or ""
    )


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
    use_cloudflare: bool = False,
    cf_base_url: str = "",
    cf_address: str = "",
    cf_admin_password: str = "",
    on_stage: StageCallback = None,
) -> Dict[str, Any]:
    require_proxy(proxy, "子号浏览器")
    from playwright.sync_api import sync_playwright

    def report(stage: str, message: str) -> None:
        if on_stage:
            try:
                on_stage(stage, message)
            except Exception:  # noqa: BLE001
                logger.debug("on_stage failed", exc_info=True)

    profile_dir = Path(settings.database_url.split("///")[-1]).resolve().parent.parent / "data" / "chrome-profiles" / email.replace("@", "_at_")
    profile_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ok": False, "email": email, "password": password, "mode": mode}
    with chrome_proxy_launch(proxy) as proxy_config, sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            **chromium_context_kwargs(profile_dir, proxy_config)
        )
        try:
            browser.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
        except Exception:  # noqa: BLE001
            pass
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.set_default_timeout(60000)
        try:
            target = start_url or "https://chatgpt.com/auth/login"
            report("browser_open", f"正在打开注册/登录页 {target}")
            page.goto(target, wait_until="load")
            if not wait_cloudflare(page):
                result["error"] = _session_failure(page, {"status": "cloudflare"})
                result["error_code"] = "cloudflare_challenge"
                result["debug_dir"] = _save_debug(page, profile_dir)
                return result
            page.wait_for_timeout(1500)
            report("fill_email", f"页面已打开：{page.title() or page.url}")
            _fill_first(page, ['input#email', 'input[name="email"]', 'input[type="email"]'], email)
            _click_exact(page, ["Continue"]) or _click_first(page, ['button[type="submit"]'])
            page.wait_for_timeout(2500)
            wait_cloudflare(page, timeout_sec=20)

            if mode == "register" and _click_exact(page, ["Create account", "Sign up"]):
                report("create_account", "正在进入创建账号")
                page.wait_for_timeout(1500)

            session: dict[str, Any] = {}
            last_url = ""
            password_tried = False
            otp_sent = False
            has_mail = bool(pickup_url or use_cloudflare)
            debug_log = Path(profile_dir).resolve().parent.parent / "debug" / "onboard.log"
            debug_log.parent.mkdir(parents=True, exist_ok=True)
            for _ in range(30):
                wait_cloudflare(page, timeout_sec=15)
                url = (page.url or "").lower()
                if url != last_url:
                    last_url = url
                    logger.info("onboard page %s", page.url)
                    report("wait_page", f"当前页面 {page.title() or page.url}")
                    try:
                        debug_log.open("a", encoding="utf-8").write(f"{time.strftime('%H:%M:%S')} {page.url} | {page.title()}\n")
                    except Exception:  # noqa: BLE001
                        pass

                if page_rate_limited(page):
                    result["error"] = "OpenAI 限流：验证码试太多次，等几分钟再点重新注册"
                    result["error_code"] = "openai_rate_limited"
                    break

                on_verify = "email-verification" in url or "check your inbox" in _page_text(page).lower()
                otp_ready = bool(_find_otp(page) or _otp_boxes(page))
                if otp_ready and (on_verify or not _visible(page, 'input[type="password"], input[name="current-password"]')):
                    report("email_otp", "等待邮箱验证码")
                    try:
                        code = _wait_mailbox_code(
                            email=email,
                            pickup_url=pickup_url,
                            proxy=proxy,
                            use_cloudflare=use_cloudflare,
                            cf_base_url=cf_base_url,
                            cf_address=cf_address,
                            cf_admin_password=cf_admin_password,
                        )
                    except Exception as exc:  # noqa: BLE001
                        result["error"] = f"email OTP failed: {exc}"
                        result["error_code"] = "mail_otp_timeout"
                        break
                    if not code:
                        result["error"] = result.get("error") or "email OTP not found"
                        result["error_code"] = result.get("error_code") or "mail_otp_timeout"
                        break
                    if not _fill_otp(page, code):
                        result["error"] = "email OTP input not found"
                        result["error_code"] = "mail_otp_timeout"
                        break
                    _click_exact(page, ["Continue", "Verify"]) or _click_first(page, ['button[type="submit"]'])
                    page.wait_for_timeout(2500)
                    continue
                if on_verify:
                    report("email_otp", "验证码页还没出现输入框，继续等")
                    page.wait_for_timeout(1500)
                    continue

                if _visible(page, 'input[type="password"], input[name="current-password"]'):
                    if has_mail and not otp_sent and _click_exact(page, [
                        "Log in with a one-time code",
                        "Email me a code",
                        "Send a code",
                        "Use a one-time code",
                    ]):
                        otp_sent = True
                        report("email_otp", "密码页改走邮箱一次性验证码")
                        page.wait_for_timeout(2500)
                        continue
                    body = _page_text(page).lower()
                    if password and not password_tried:
                        report("password", "正在填写密码")
                        _fill_first(
                            page,
                            ['input[name="current-password"]', 'input[type="password"]', 'input[name="password"]'],
                            password,
                        )
                        _click_exact(page, ["Continue"]) or _click_first(page, ['button[type="submit"]'])
                        password_tried = True
                        page.wait_for_timeout(2500)
                        continue
                    password_bad = (not password) or any(token in body for token in _PASSWORD_ERRORS)
                    if not password:
                        result["error"] = "登录页要密码，但子号没有保存密码，也没有邮箱验证码入口"
                        result["error_code"] = "password_missing"
                        break
                    page.wait_for_timeout(1200)
                    continue

                if _visible(page, 'input[type="tel"]') or "add-phone" in url:
                    report("add_phone", "页面要求添加手机号")
                    if not phone or not sms_url:
                        result["error"] = "需要接码，但未提供手机号"
                        result["error_code"] = "sms_missing"
                        break
                    digits = "".join(ch for ch in phone if ch.isdigit())
                    national = digits[1:] if digits.startswith("1") and len(digits) == 11 else digits
                    _fill_first(page, ['input[type="tel"]', 'input[name="phone"]'], national)
                    _click_exact(page, ["Text", "SMS", "Send", "Continue"])
                    _click_first(page, ['button[type="submit"]', 'label:has-text("Text")'])
                    page.wait_for_timeout(2500)
                    report("sms_otp", "等待短信验证码")
                    sms_code = sms_client.wait_for_code(sms_url, proxy=proxy, timeout_sec=90)
                    otp_el = _find_otp(page)
                    if otp_el:
                        otp_el.fill(sms_code)
                    _click_exact(page, ["Continue", "Verify"]) or _click_first(page, ['button[type="submit"]'])
                    page.wait_for_timeout(2500)
                    continue

                if _visible(page, 'input[name="name"], input[name="fullName"], input[name="firstName"]'):
                    report("profile", "正在填写资料")
                    _fill_first(page, ['input[name="name"]', 'input[name="fullName"]'], "James Smith")
                    _fill_first(page, ['input[name="firstName"]'], "James")
                    _fill_first(page, ['input[name="lastName"]'], "Smith")
                    _fill_first(page, ['input[name="birthdate"]', 'input[name="age"]'], "28")
                    _click_exact(page, ["Finish creating account", "Continue"]) or _click_first(page, ['button[type="submit"]'])
                    page.wait_for_timeout(2500)
                    continue

                if _accept_terms(page):
                    report("terms", "已勾选服务条款")
                    page.wait_for_timeout(1500)
                    continue

                peeked = _peek_session(page)
                if session_access_token(peeked):
                    session = peeked
                    break
                polled = _poll_session(page)
                if session_access_token(polled):
                    session = polled
                    break
                title = (page.title() or "").lower()
                if "chat, work, create" in title and _click_exact(page, ["Log in"]):
                    report("login", "未登录首页，点 Log in 回去")
                    page.wait_for_timeout(2000)
                    continue
                if "auth.openai.com" in url:
                    page.wait_for_timeout(1200)
                    continue
                if _click_exact(page, ["Accept", "Accept invite", "Join workspace", "Join", "I agree", "Okay", "Next"]):
                    page.wait_for_timeout(1500)
                    continue
                page.wait_for_timeout(1200)

            result["debug_dir"] = _save_debug(page, profile_dir)
            report("session", f"正在读取登录态 {page.url or ''}")
            if not session_access_token(session):
                session = _extract_session(page)
            access_token = session_access_token(session)
            js = session.get("json") if isinstance(session.get("json"), dict) else {}
            session_token = str((js or {}).get("sessionToken") or "").strip()
            user = session_user(session)
            result.update({
                "ok": bool(access_token),
                "access_token": access_token,
                "session_token": session_token,
                "account_id": str((user or {}).get("id") or ""),
                "final_url": page.url or "",
            })
            if not access_token:
                result["debug_dir"] = _save_debug(page, profile_dir)
                result["error"] = result.get("error") or _session_failure(page, session)
                result["error_code"] = result.get("error_code") or (
                    "cloudflare_challenge" if page_is_cloudflare(page) else "session_missing"
                )
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
            result["error_code"] = result.get("error_code") or "browser_failed"
            result["ok"] = False
            try:
                result["debug_dir"] = _save_debug(page, profile_dir)
            except Exception:  # noqa: BLE001
                pass
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return result
