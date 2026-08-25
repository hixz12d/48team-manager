"""Chrome CDP + Playwright 注册 / 复用登录。强制走子号 ISP。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.config import settings
from app.services.mail_otp import wait_for_mailbox_item
from app.services.sms import chrome_proxy_config, require_proxy, sms_client

logger = logging.getLogger(__name__)

StageCallback = Optional[Callable[[str, str], None]]


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


def _find_otp(page):
    for selector in [
        'input[name="code"]',
        'input[autocomplete="one-time-code"]',
        'input[name="otp"]',
        'input[placeholder*="Code" i]',
    ]:
        loc = page.locator(selector)
        try:
            if loc.count() > 0 and loc.first.is_visible():
                return loc.first
        except Exception:  # noqa: BLE001
            continue
    return None


def _extract_session(page) -> dict[str, Any]:
    try:
        page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1200)
    except Exception:  # noqa: BLE001
        pass
    try:
        return page.evaluate(
            """async () => {
              const r = await fetch('/api/auth/session', {credentials:'include', cache:'no-store'});
              const text = await r.text();
              let json = null;
              try { json = JSON.parse(text); } catch (e) {}
              return {status: r.status, json};
            }"""
        ) or {}
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}


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
    with sync_playwright() as playwright:
        launch_kwargs = {
            "user_data_dir": str(profile_dir),
            "headless": bool(settings.browser_headless),
            "proxy": chrome_proxy_config(proxy),
            "locale": "en-US",
            "viewport": {"width": 1280, "height": 900},
            "args": ["--disable-features=Translate", "--disable-dev-shm-usage"],
        }
        if settings.browser_channel:
            launch_kwargs["channel"] = settings.browser_channel
        browser = playwright.chromium.launch_persistent_context(**launch_kwargs)
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.set_default_timeout(60000)
        try:
            target = start_url or "https://chatgpt.com/auth/login"
            report("browser_open", "正在打开注册/登录页")
            page.goto(target, wait_until="domcontentloaded")
            page.wait_for_timeout(2000)
            report("fill_email", "正在填写邮箱")
            _fill_first(page, ['input[name="email"]', 'input[type="email"]'], email)
            _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Next")'])
            page.wait_for_timeout(2500)

            if mode == "register":
                report("create_account", "正在进入创建账号")
                _click_first(page, ['button:has-text("Create account")', 'button:has-text("Sign up")', 'a:has-text("Create account")'])
                page.wait_for_timeout(1500)

            for _ in range(18):
                url = (page.url or "").lower()
                otp_el = _find_otp(page)
                if otp_el:
                    report("email_otp", "等待邮箱验证码")
                    code = ""
                    if pickup_url or use_cloudflare:
                        try:
                            code = wait_for_mailbox_item(
                                email=email,
                                pickup_url=pickup_url,
                                proxy=proxy,
                                kind="code",
                                timeout_sec=90,
                                cf_base_url=cf_base_url if use_cloudflare else "",
                                cf_address=cf_address if use_cloudflare else "",
                                cf_admin_password=cf_admin_password if use_cloudflare else "",
                            ) or ""
                        except Exception as exc:  # noqa: BLE001
                            result["error"] = f"email OTP failed: {exc}"
                            result["error_code"] = "mail_otp_timeout"
                    if not code:
                        result["error"] = result.get("error") or "email OTP not found"
                        result["error_code"] = result.get("error_code") or "mail_otp_timeout"
                        break
                    otp_el.fill(code)
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")'])
                    page.wait_for_timeout(2500)
                    continue

                if page.locator('input[type="password"]').count() > 0:
                    report("password", "正在填写密码")
                    _fill_first(page, ['input[type="password"]', 'input[name="password"]'], password)
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")'])
                    page.wait_for_timeout(2500)
                    continue

                if page.locator('input[type="tel"]').count() > 0 or "add-phone" in url:
                    report("add_phone", "页面要求添加手机号")
                    if not phone or not sms_url:
                        result["error"] = "需要接码，但未提供手机号"
                        result["error_code"] = "sms_missing"
                        break
                    digits = "".join(ch for ch in phone if ch.isdigit())
                    national = digits[1:] if digits.startswith("1") and len(digits) == 11 else digits
                    _fill_first(page, ['input[type="tel"]', 'input[name="phone"]'], national)
                    _click_first(page, ['button:has-text("Text")', 'button:has-text("SMS")', 'label:has-text("Text")'])
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Send")'])
                    page.wait_for_timeout(2500)
                    report("sms_otp", "等待短信验证码")
                    sms_code = sms_client.wait_for_code(sms_url, proxy=proxy, timeout_sec=90)
                    otp_el = _find_otp(page)
                    if otp_el:
                        otp_el.fill(sms_code)
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")'])
                    page.wait_for_timeout(2500)
                    continue

                if page.locator('input[name="name"], input[name="fullName"]').count() > 0:
                    report("profile", "正在填写资料")
                    _fill_first(page, ['input[name="name"]', 'input[name="fullName"]'], "James Smith")
                    _fill_first(page, ['input[name="birthdate"]', 'input[name="age"]'], "28")
                    _click_first(page, ['button:has-text("Finish creating account")', 'button[type="submit"]', 'button:has-text("Continue")'])
                    page.wait_for_timeout(2500)
                    continue

                if "chatgpt.com" in url and "auth" not in url and "login" not in url:
                    break
                if _click_first(page, ['button:has-text("Continue")', 'button:has-text("Accept")', 'button:has-text("I agree")', 'button:has-text("Okay")']):
                    page.wait_for_timeout(1500)
                    continue
                page.wait_for_timeout(1200)

            report("session", "正在读取登录态")
            session = _extract_session(page)
            js = session.get("json") if isinstance(session.get("json"), dict) else {}
            access_token = str((js or {}).get("accessToken") or (js or {}).get("access_token") or "").strip()
            session_token = str((js or {}).get("sessionToken") or "").strip()
            user = (js or {}).get("user") if isinstance((js or {}).get("user"), dict) else {}
            result.update({
                "ok": bool(access_token),
                "access_token": access_token,
                "session_token": session_token,
                "account_id": str((user or {}).get("id") or ""),
                "final_url": page.url or "",
            })
            if not access_token:
                result["error"] = result.get("error") or "no accessToken in session"
                result["error_code"] = result.get("error_code") or "session_missing"
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
            result["error_code"] = result.get("error_code") or "browser_failed"
            result["ok"] = False
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return result
