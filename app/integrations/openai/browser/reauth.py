"""用 Playwright 跑完 OpenAI OAuth，给 iCloud 子号自动重新授权。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.core.config import load_settings
from app.integrations.openai.browser.onboard import (
    ABOUT_YOU_STUCK_LIMIT,
    _accept_terms,
    _click_exact,
    _click_first,
    _fill_about_you,
    _fill_first,
    _fill_phone_number,
    _submit_phone_sms,
    _find_otp,
    _mail_kwargs,
    _page_text,
    _pick_workspace,
    _save_debug,
    _snapshot_mailbox_codes,
    _visible,
    chromium_context_kwargs,
    goto_with_retries,
    looks_like_about_you,
    looks_like_session_ended,
    page_phone_rejection,
    phone_page_outcome,
    record_pool_phone,
    release_pool_phone,
    split_phone,
    take_pool_phone,
    wait_cloudflare,
    wait_email_otp_with_resend,
)
from app.domain.reauth import is_oauth_callback, looks_like_deactivated
from app.integrations.proxy.socks_bridge import chrome_proxy_launch
from app.integrations.sms.client import require_proxy, sms_client

settings = load_settings()

logger = logging.getLogger(__name__)

StageCallback = Optional[Callable[[str, str], None]]


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
    on_stage: StageCallback = None,
    phone_source=None,
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
        Path(__file__).resolve().parents[4]
        / "data"
        / "chrome-profiles"
        / email.replace("@", "_at_")
    )
    profile_dir.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {"ok": False, "email": email, "callback_url": "", "phone": phone, "sms_url": sms_url}
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

        def attach(target) -> None:
            try:
                target.route("http://localhost:1455/**", fulfill_callback)
                target.route("http://127.0.0.1:1455/**", fulfill_callback)
                target.on("framenavigated", lambda frame: remember(frame.url or ""))
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
            report("browser_open", "正在打开 Codex 授权链接")
            goto_with_retries(page, authorize_url, report=report)
            wait_cloudflare(page)
            page.wait_for_timeout(1500)
            remember(page.url or "")
            known_codes = _snapshot_mailbox_codes(
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
            otp_resends = 0
            password_tried = False
            signup_clicked = False
            about_you_tries = 0

            for _ in range(36):
                remember(page.url or "")
                if captured["url"]:
                    break

                url = (page.url or "").lower()
                page_body = _page_text(page)
                if looks_like_deactivated(title=page.title() or "", body=page_body, url=page.url or ""):
                    result["error"] = "账号已被 deactivate，停止重授权"
                    result["error_code"] = "account_deactivated"
                    report("deactivated", result["error"])
                    break
                if looks_like_about_you(title=page.title() or "", body=page_body, url=url):
                    about_you_tries += 1
                    if about_you_tries > ABOUT_YOU_STUCK_LIMIT:
                        result["error"] = "卡在年龄/生日页，没有进入下一步"
                        result["error_code"] = "about_you_stuck"
                        break
                    report("about_you", f"验证码已过，正在填写年龄（第 {about_you_tries} 次）")
                    if not _fill_about_you(page):
                        page.wait_for_timeout(1500)
                    continue
                if looks_like_session_ended(title=page.title() or "", body=_page_text(page)):
                    report("session_ended", "授权页会话结束，点 Log in")
                    _click_first(page, ['button:has-text("Log in")', 'a:has-text("Log in")'])
                    page.wait_for_timeout(2000)
                    continue
                if "you're all set" in _page_text(page).lower() or "you’re all set" in _page_text(page).lower():
                    report("all_set", "注册完成页，点 Continue")
                    _click_first(page, ['button:has-text("Continue")'])
                    page.wait_for_timeout(2000)
                    continue
                if _pick_workspace(page):
                    report("workspace", "已选择工作空间")
                    page.wait_for_timeout(1500)
                    continue
                on_phone = any(bit in url for bit in ("add-phone", "phone-verification")) or _visible(page, 'input[type="tel"]')
                if on_phone:
                    report("add_phone", "授权页要求手机号/短信验证码")
                    outcome, msg = phone_page_outcome(page, allow_risk=False)
                    if outcome in {"invalid", "recently_used", "risk"}:
                        if phone_source:
                            record_pool_phone(phone_source, outcome, msg)
                            report("add_phone", f"换号原因：{msg}")
                            number, url, error, code = take_pool_phone(phone_source, report)
                            if error:
                                result["error"] = error
                                result["error_code"] = code
                                break
                            phone, sms_url = number, url
                            continue
                        result["error"] = msg or page_phone_rejection(page) or "手机号不被 OpenAI 接受"
                        result["error_code"] = "sms_rejected"
                        break
                    if not phone or not sms_url:
                        if phone_source:
                            number, url, error, code = take_pool_phone(phone_source, report)
                            if error:
                                result["error"] = error
                                result["error_code"] = code
                                break
                            phone, sms_url = number, url
                        elif _click_exact(page, ["Skip", "Not now", "Maybe later", "Skip for now", "I'll do this later"]):
                            report("add_phone", "手机号页已跳过")
                            page.wait_for_timeout(1500)
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
                        country, _national = split_phone(phone)
                        filled = _fill_phone_number(page, phone)
                        if not filled and country == "China":
                            msg = "接码是 +86，但授权页停在美国 +1。Codex 不吃这个号，换能过的 +1 接码"
                            if phone_source:
                                record_pool_phone(phone_source, "invalid", msg)
                                report("add_phone", f"换号原因：{msg}")
                                number, url, error, code = take_pool_phone(phone_source, report)
                                if error:
                                    result["error"] = error
                                    result["error_code"] = code
                                    break
                                phone, sms_url = number, url
                                continue
                            result["error"] = msg
                            result["error_code"] = "sms_rejected"
                            break
                        _submit_phone_sms(page)
                        page.wait_for_timeout(2500)
                        outcome, msg = phone_page_outcome(page)
                        if outcome in {"invalid", "recently_used", "risk"}:
                            if phone_source:
                                record_pool_phone(phone_source, outcome, msg)
                                report("add_phone", f"换号原因：{msg}")
                                number, url, error, code = take_pool_phone(phone_source, report)
                                if error:
                                    result["error"] = error
                                    result["error_code"] = code
                                    break
                                phone, sms_url = number, url
                                continue
                            result["error"] = msg or page_phone_rejection(page)
                            result["error_code"] = "sms_rejected"
                            break
                        continue
                    otp_el = _find_otp(page)
                    if otp_el:
                        report("sms_otp", "等待短信验证码")
                        try:
                            sms_code = sms_client.wait_for_code(sms_url, proxy=proxy, timeout_sec=90)
                        except TimeoutError as exc:
                            msg = str(exc) or "接码超时或空号"
                            if phone_source:
                                record_pool_phone(phone_source, "no_sms", msg)
                                report("add_phone", f"换号原因：{msg}")
                                number, url, error, code = take_pool_phone(phone_source, report)
                                if error:
                                    result["error"] = error
                                    result["error_code"] = code or "sms_failed"
                                    break
                                phone, sms_url = number, url
                                continue
                            result["error"] = msg
                            result["error_code"] = "sms_failed"
                            break
                        otp_el.fill(sms_code)
                        _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")'])
                        page.wait_for_timeout(2500)
                        record_pool_phone(phone_source, "success")
                        result["phone"] = phone
                        result["sms_url"] = sms_url
                    else:
                        page.wait_for_timeout(1500)
                    continue

                password_boxes = _visible_password_inputs(page)

                if allow_signup and password_boxes and not signup_clicked and (
                    _click_exact(page, ["Sign up", "Create account", "Create an account"])
                    or _click_first(page, ['a:has-text("Sign up")', 'button:has-text("Sign up")'])
                ):
                    signup_clicked = True
                    report("create_account", "密码页改走注册")
                    page.wait_for_timeout(1500)
                    continue

                if allow_signup and not password_tried and (
                    _click_exact(page, ["Continue with password", "Try again"])
                    or _click_first(page, ['button:has-text("Continue with password")', 'button:has-text("Try again")'])
                ):
                    report("create_password", "授权注册需要密码，不走无密码验证码")
                    page.wait_for_timeout(1500)
                    password_boxes = _visible_password_inputs(page)

                if allow_signup and password_boxes and not password_tried:
                    report("password", "正在设置注册密码")
                    for box in password_boxes:
                        try:
                            box.fill(password)
                        except Exception:  # noqa: BLE001
                            continue
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Create account")'])
                    password_tried = True
                    page.wait_for_timeout(2500)
                    continue

                otp_el = _find_otp(page)
                if otp_el:
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
                    otp_el.fill(code)
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Verify")'])
                    known_codes.add(code)
                    otp_submits += 1
                    report("email_otp", f"已提交验证码（第 {otp_submits} 次）")
                    page.wait_for_timeout(4000)
                    continue

                on_verify = "email-verification" in url or "check your inbox" in _page_text(page).lower()
                if on_verify and not password_boxes:
                    report("email_otp", "验证码页还没出现输入框，继续等")
                    page.wait_for_timeout(1500)
                    continue

                if password_boxes:
                    if allow_signup and password_tried:
                        page.wait_for_timeout(1500)
                        continue
                    body = _page_text(page).lower()
                    missing_password = not str(password or "").strip()
                    bad_password = password_tried or missing_password or any(
                        token in body for token in ("incorrect", "wrong password", "did not match")
                    )
                    if (missing_password or bad_password) and (
                        _click_exact(page, ["Log in with a one-time code", "Email me a code", "Send a code", "Use a one-time code"])
                        or _click_first(page, ['button:has-text("one-time code")', 'button:has-text("Email me a code")'])
                    ):
                        report("email_otp", "没有本地密码，改走一次性验证码" if missing_password else "密码不对，改走一次性验证码")
                        page.wait_for_timeout(2500)
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
                    for box in password_boxes:
                        try:
                            box.fill(password)
                        except Exception:  # noqa: BLE001
                            continue
                    _click_first(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Create account")'])
                    password_tried = True
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

                if _accept_terms(page):
                    report("terms", "已勾选服务条款")
                    page.wait_for_timeout(1500)
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
            if not captured["url"] and not result.get("error"):
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
                result["debug_dir"] = _save_debug(page, profile_dir)
        except Exception as exc:  # noqa: BLE001
            result["error"] = str(exc)
            result["error_code"] = result.get("error_code") or "browser_failed"
            result["ok"] = False
        finally:
            result["phone"] = phone
            result["sms_url"] = sms_url
            release_pool_phone(phone_source)
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass
    return result
