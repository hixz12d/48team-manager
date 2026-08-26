"""用 Playwright 跑完 OpenAI OAuth，给 iCloud 子号自动重新授权。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.config import settings
from app.services.browser_onboard import _click_first, _fill_first, _find_otp, chromium_context_kwargs, wait_cloudflare
from app.services.mail_otp import wait_for_mailbox_item
from app.services.reauth import is_oauth_callback
from app.services.sms import require_proxy, sms_client
from app.services.socks_bridge import chrome_proxy_launch

logger = logging.getLogger(__name__)

StageCallback = Optional[Callable[[str, str], None]]


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
    on_stage: StageCallback = None,
) -> Dict[str, Any]:
    require_proxy(proxy, "子号浏览器")
    if not authorize_url:
        return {"ok": False, "error": "缺少授权链接", "error_code": "oauth_url_missing"}

    from playwright.sync_api import sync_playwright

    def report(stage: str, message: str) -> None:
        if on_stage:
            try:
                on_stage(stage, message)
            except Exception:  # noqa: BLE001
                logger.debug("on_stage failed", exc_info=True)

    profile_dir = (
        Path(settings.database_url.split("///")[-1]).resolve().parent.parent
        / "data"
        / "chrome-profiles"
        / email.replace("@", "_at_")
    )
    profile_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ok": False, "email": email, "callback_url": ""}
    captured = {"url": ""}

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

    with chrome_proxy_launch(proxy) as proxy_config, sync_playwright() as playwright:
        browser = playwright.chromium.launch_persistent_context(
            **chromium_context_kwargs(profile_dir, proxy_config)
        )
        page = browser.pages[0] if browser.pages else browser.new_page()
        page.set_default_timeout(60000)
        try:
            page.route("http://localhost:1455/**", fulfill_callback)
            page.route("http://127.0.0.1:1455/**", fulfill_callback)
            page.on("framenavigated", lambda frame: remember(frame.url or ""))
            report("browser_open", "正在打开 ChatGPT 授权页")
            page.goto(authorize_url, wait_until="load")
            wait_cloudflare(page)
            page.wait_for_timeout(1500)
            remember(page.url or "")

            for _ in range(24):
                remember(page.url or "")
                if captured["url"]:
                    break

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

                if page.locator('input[type="email"], input[name="email"], input[name="username"]').count() > 0:
                    report("fill_email", "正在填写邮箱")
                    _fill_first(
                        page,
                        ['input[name="email"]', 'input[type="email"]', 'input[name="username"]'],
                        email,
                    )
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Next")'])
                    page.wait_for_timeout(2500)
                    continue

                if page.locator('input[type="tel"]').count() > 0 or "add-phone" in url or "phone" in url:
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

                if _click_first(
                    page,
                    [
                        'button:has-text("Continue with email")',
                        'button:has-text("Log in")',
                        'a:has-text("Log in")',
                        'button:has-text("Use a different")',
                        'button:has-text("Use another")',
                    ],
                ):
                    page.wait_for_timeout(1500)
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
                ):
                    page.wait_for_timeout(1500)
                    continue
                page.wait_for_timeout(1200)

            remember(page.url or "")
            if not captured["url"]:
                for _ in range(20):
                    page.wait_for_timeout(1000)
                    remember(page.url or "")
                    if captured["url"]:
                        break

            callback = captured["url"]
            result.update({
                "ok": bool(callback),
                "callback_url": callback,
                "final_url": page.url or "",
            })
            if not callback:
                result["error"] = result.get("error") or "没有拿到 OAuth 回调"
                result["error_code"] = result.get("error_code") or "oauth_callback_missing"
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
