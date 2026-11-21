"""Reusable Playwright login flow for chatgpt.com (shared by onboard and desktop OAuth)."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable, Optional

from app.core.jwt import jwt_parser
from app.domain.reauth import looks_like_deactivated
from app.integrations.openai.browser.onboard import (
    ABOUT_YOU_STUCK_LIMIT,
    EMAIL_INPUT_SELECTOR,
    LOGIN_START_URL,
    NOT_REGISTERED_ERROR,
    _PASSWORD_ERRORS,
    _accept_terms,
    _click_exact,
    _click_first,
    _email_input_visible,
    _extract_session,
    _fill_about_you,
    _fill_email,
    _fill_first,
    _fill_otp,
    _fill_phone_number,
    _find_otp,
    _mail_kwargs,
    _otp_boxes,
    _page_past_otp,
    _page_text,
    _peek_session,
    _pick_workspace,
    _poll_session,
    _save_debug,
    _session_failure,
    _snapshot_mailbox_codes,
    _submit_phone_sms,
    _visible,
    _wait_and_fallback_submit_otp,
    can_peek_session,
    goto_with_retries,
    looks_like_about_you,
    looks_like_email_gate,
    looks_like_registration,
    looks_like_session_ended,
    page_is_cloudflare,
    page_rate_limited,
    phone_page_outcome,
    record_pool_phone,
    session_access_token,
    session_user,
    take_pool_phone,
    wait_cloudflare,
    wait_email_otp_with_resend,
)
from app.integrations.sms.client import sms_client

logger = logging.getLogger(__name__)

StageCallback = Optional[Callable[[str, str], None]]
CHATGPT_HOME_URL = "https://chatgpt.com/"


def session_email(session: dict[str, Any] | None) -> str:
    """Extract normalized email from /api/auth/session user block or accessToken JWT."""
    email = str(session_user(session).get("email") or "").strip().lower()
    if email:
        return email
    token = session_access_token(session)
    if token:
        try:
            return str(jwt_parser.extract_email(token) or "").strip().lower()
        except Exception:  # noqa: BLE001
            return ""
    return ""


def peek_chatgpt_session(page) -> dict[str, Any]:
    """Read /api/auth/session from page context first, falling back to page.request."""
    peeked = _peek_session(page)
    if session_access_token(peeked):
        return peeked
    polled = _poll_session(page)
    if session_access_token(polled):
        return polled
    return polled if polled.get("status") else peeked or polled


def _has_active_auth_form(page) -> bool:
    url = (getattr(page, "url", "") or "").lower()
    if _email_input_visible(page):
        return True
    if _visible(page, 'input[type="password"], input[name="current-password"]'):
        return True
    if _find_otp(page) or _otp_boxes(page):
        return True
    if _visible(page, 'input[type="tel"]') or "add-phone" in url or "phone-verification" in url:
        return True
    if "about-you" in url or looks_like_about_you(title=page.title() or "", body=_page_text(page), url=url):
        return True
    return False


def _click_login_entry(page) -> bool:
    return (
        _click_first(page, ['[data-testid="login-button"]'])
        or _click_exact(page, ["Log in", "Sign in", "登录"])
        or _click_first(page, ['button:has-text("Log in")', 'a:has-text("Log in")'])
    )


def _click_one_time_code(page) -> bool:
    return (
        _click_exact(page, [
            "Log in with a one-time code",
            "Email me a code",
            "Send a code",
            "Use a one-time code",
            "使用一次性验证码登录",
            "发送验证码",
        ])
        or _click_first(page, [
            'button:has-text("one-time code")',
            'button:has-text("Email me a code")',
            'a:has-text("one-time code")',
            'button:has-text("一次性验证码")',
        ])
    )


def wait_chatgpt_session(
    page,
    target_email: str,
    *,
    timeout_sec: float = 30.0,
    report: StageCallback = None,
    break_on_auth_form: bool = False,
) -> dict[str, Any]:
    """Poll /api/auth/session until accessToken + target email appear, or timeout."""
    wanted = str(target_email or "").strip().lower()
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    last_session: dict[str, Any] = {}
    if report:
        report("chatgpt_session", "正在确认 ChatGPT 登录态")
    while True:
        if page_is_cloudflare(page):
            remaining = max(1, int(deadline - time.monotonic()))
            if not wait_cloudflare(page, timeout_sec=min(remaining, 15)):
                if time.monotonic() >= deadline:
                    return {
                        "status": "cloudflare",
                        "error_code": "cloudflare_challenge",
                        "error": _session_failure(page, {"status": "cloudflare"}),
                        "session": last_session,
                    }
        url = (getattr(page, "url", "") or "").lower()
        if break_on_auth_form and (not can_peek_session(url) or _has_active_auth_form(page)):
            return {"status": "navigated_away", "session": last_session}
        sess = peek_chatgpt_session(page)
        if sess:
            last_session = sess
        if session_access_token(sess):
            actual = session_email(sess)
            if actual == wanted:
                return {"status": "ok", "session": sess, "email": actual}
            if actual:
                return {
                    "status": "mismatch",
                    "error_code": "identity_mismatch",
                    "error": "ChatGPT 登录态邮箱与目标邮箱不一致，未自动退出",
                    "session": sess,
                    "email": actual,
                }
        if time.monotonic() >= deadline:
            break
        page.wait_for_timeout(1000)
    if page_is_cloudflare(page):
        return {
            "status": "cloudflare",
            "error_code": "cloudflare_challenge",
            "error": _session_failure(page, {"status": "cloudflare"}),
            "session": last_session,
        }
    return {
        "status": "timeout",
        "error_code": "registration_home_not_ready",
        "error": "登录后未在 30 秒内确认目标邮箱的 ChatGPT 会话就绪",
        "session": last_session,
    }


def run_chatgpt_login_loop(
    page,
    *,
    email: str,
    password: str = "",
    pickup_url: str = "",
    phone: str = "",
    sms_url: str = "",
    proxy: str = "",
    start_url: str = "",
    mode: str = "login",
    team_name: str = "",
    use_cloudflare: bool = False,
    cf_base_url: str = "",
    cf_address: str = "",
    cf_admin_password: str = "",
    hme_base_url: str = "",
    hme_service_token: str = "",
    hme_account_id: str = "",
    profile_dir: Path | None = None,
    phone_source=None,
    allow_sms: bool = True,
    allow_profile_setup: bool | None = None,
    invite_entry: bool = False,
    require_session_match: bool = False,
    initial_goto: bool = True,
    session_wait_sec: float = 30.0,
    report: StageCallback = None,
) -> dict[str, Any]:
    """Shared chatgpt.com login / register Playwright loop.

    Used by onboard.py (register/relogin/login) and ensure_chatgpt_login (desktop OAuth pre-login).
    """
    def emit(stage: str, message: str) -> None:
        if report:
            report(stage, message)

    wanted_email = str(email or "").strip().lower()
    profile_setup_allowed = (mode != "login") if allow_profile_setup is None else bool(allow_profile_setup)
    result: dict[str, Any] = {
        "ok": False,
        "email": email,
        "password": password,
        "mode": mode,
        "phone": phone,
        "sms_url": sms_url,
        "sms_code_submissions": 0,
    }

    if initial_goto:
        target = start_url or LOGIN_START_URL
        emit("browser_open", "正在打开邀请注册页" if invite_entry else "正在打开注册/登录页")
        goto_with_retries(page, target, report=report)
        if not wait_cloudflare(page):
            result["error"] = _session_failure(page, {"status": "cloudflare"})
            result["error_code"] = "cloudflare_challenge"
            if profile_dir is not None:
                result["debug_dir"] = _save_debug(page, profile_dir)
            return result
        page.wait_for_timeout(1500)
        try:
            page.wait_for_selector(EMAIL_INPUT_SELECTOR, timeout=15000)
        except Exception:  # noqa: BLE001
            pass

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
    known_codes = _snapshot_mailbox_codes(**mail_kw)
    if initial_goto:
        emit("fill_email", f"页面已打开：{page.title() or page.url}")
    if _email_input_visible(page):
        if not _fill_email(page, email):
            emit("fill_email", "邮箱框在，但没填进去，后面会再试")
        else:
            _click_exact(page, ["Continue"]) or _click_first(page, ['button[type="submit"]'])
            page.wait_for_timeout(2500)
    elif require_session_match and can_peek_session(page.url or ""):
        emit("chatgpt_login", "未检测到已登录会话，正在打开 ChatGPT 登录")
        if _click_login_entry(page):
            page.wait_for_timeout(2500)
        else:
            goto_with_retries(page, LOGIN_START_URL, report=report)
            page.wait_for_timeout(1500)
    wait_cloudflare(page, timeout_sec=20)

    if mode == "register" and _click_exact(page, ["Create account", "Sign up"]):
        emit("create_account", "正在进入创建账号")
        page.wait_for_timeout(1500)

    session: dict[str, Any] = {}
    last_url = ""
    password_tried = False
    otp_sent = False
    otp_submits = 0
    otp_resends = 0
    email_submits = 0
    about_you_tries = 0
    login_clicks = 0
    invite_reopened = False
    has_mail = bool(pickup_url or use_cloudflare or hme_service_token)
    debug_log = None
    if profile_dir is not None and not require_session_match:
        base = profile_dir.resolve().parent.parent
        if profile_dir.resolve().parent.name == "chromix":
            base = base.parent
        debug_log = base / "debug" / "onboard.log"
        debug_log.parent.mkdir(parents=True, exist_ok=True)

    for _ in range(36):
        wait_cloudflare(page, timeout_sec=15)
        if page_is_cloudflare(page) and not wait_cloudflare(page, timeout_sec=30):
            result["error"] = _session_failure(page, {"status": "cloudflare"})
            result["error_code"] = "cloudflare_challenge"
            break
        url = (page.url or "").lower()
        page_title = page.title() or ""
        if url != last_url:
            last_url = url
            logger.info("onboard page %s", page.url)
            emit("wait_page", f"当前页面 {page_title or page.url}")
            if debug_log is not None:
                try:
                    debug_log.open("a", encoding="utf-8").write(f"{time.strftime('%H:%M:%S')} {page.url} | {page_title}\n")
                except Exception:  # noqa: BLE001
                    pass

        page_text = _page_text(page)
        if looks_like_deactivated(title=page_title, body=page_text, url=page.url or ""):
            result["error"] = "账号已被 deactivate，停止登录"
            result["error_code"] = "account_deactivated"
            emit("deactivated", result["error"])
            break

        if page_rate_limited(page):
            result["error"] = "OpenAI 限流：验证码试太多次，等几分钟再试"
            result["error_code"] = "openai_rate_limited"
            break

        on_about_you = (
            "about-you" in url
            or looks_like_about_you(title=page_title, body=page_text, url=url)
            or _visible(page, 'input[name="age"], input[placeholder*="Age" i], select[name="year"], select[autocomplete="bday-year"]')
            or ("auth.openai.com" in url and _visible(page, 'input[name="name"], input[name="fullName"], input[name="firstName"]'))
        )
        if mode == "login" and not profile_setup_allowed and (
            looks_like_registration(url=url, body=page_text) or on_about_you
        ):
            result["error"] = NOT_REGISTERED_ERROR
            result["error_code"] = "account_not_registered"
            break

        if mode == "login" and looks_like_session_ended(title=page_title, body=page_text):
            emit("session_ended", "OpenAI 会话已结束，改走登录页")
            if not _click_exact(page, ["Log in", "Continue"]):
                page.goto(LOGIN_START_URL, wait_until="load")
            page.wait_for_timeout(2000)
            continue
        if looks_like_session_ended(title=page_title, body=page_text):
            emit("session_ended", "OpenAI 会话已结束，改走登录/注册页")
            if not _click_exact(page, ["Log in", "Sign up", "Continue"]):
                page.goto(LOGIN_START_URL, wait_until="load")
            page.wait_for_timeout(2000)
            continue

        if "you're all set" in page_text.lower() or "you’re all set" in page_text.lower():
            emit("all_set", "资料完成页，点 Continue")
            _click_first(page, ['button:has-text("Continue")'])
            page.wait_for_timeout(2000)
            continue

        if (
            _email_input_visible(page)
            and not _visible(page, 'input[type="password"], input[name="current-password"]')
            and not _find_otp(page)
            and not _otp_boxes(page)
        ):
            if email_submits >= 5:
                result["error"] = "卡在 ChatGPT 邮箱页，没有进入 OpenAI 登录"
                result["error_code"] = "email_gate_stuck"
                break
            emit("fill_email", f"正在提交邮箱（第 {email_submits + 1} 次）")
            if not _fill_email(page, email):
                result["error"] = "登录页找不到可用的邮箱输入框"
                result["error_code"] = "email_input_missing"
                break
            _click_exact(page, ["Continue"]) or _click_first(page, ['button[type="submit"]'])
            email_submits += 1
            page.wait_for_timeout(3000)
            continue

        if looks_like_about_you(title=page_title, body=page_text, url=url) or _visible(
            page,
            'input[name="age"], input[placeholder*="Age" i], select[name="year"], select[autocomplete="bday-year"]',
        ):
            about_you_tries += 1
            if about_you_tries > ABOUT_YOU_STUCK_LIMIT:
                result["error"] = "卡在年龄/生日页，没有进入下一步"
                result["error_code"] = "about_you_stuck"
                break
            emit("about_you", f"验证码已过，正在填写资料（第 {about_you_tries} 次）")
            if not _fill_about_you(page):
                page.wait_for_timeout(1500)
            continue

        on_verify = "email-verification" in url or "check your inbox" in page_text.lower()
        otp_ready = bool(_find_otp(page) or _otp_boxes(page))
        if otp_ready and (on_verify or not _visible(page, 'input[type="password"], input[name="current-password"]')):
            if otp_submits >= 2:
                result["error"] = "邮箱验证码提交后仍未通过，没有继续连交"
                result["error_code"] = "mail_otp_rejected"
                break
            emit("email_otp", "等待新的邮箱验证码" if otp_submits or otp_resends else "等待邮箱验证码")
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
            )
            if otp_error:
                result["error"] = otp_error
                result["error_code"] = "mail_otp_timeout"
                break
            if not code:
                continue
            started_url = page.url or ""
            if not _fill_otp(page, code):
                if _page_past_otp(page):
                    emit("email_otp", "验证码输入框已消失，页面已过验证")
                    continue
                result["error"] = "email OTP input not found"
                result["error_code"] = "mail_otp_timeout"
                break
            _wait_and_fallback_submit_otp(
                page,
                started_url=started_url,
                exact_names=["Continue", "Verify"],
                selectors=['button[type="submit"]'],
            )
            known_codes.add(code)
            otp_submits += 1
            emit("email_otp", f"已提交验证码（第 {otp_submits} 次）")
            page.wait_for_timeout(2000)
            continue
        if on_verify:
            emit("email_otp", "验证码页还没出现输入框，继续等")
            page.wait_for_timeout(1500)
            continue

        if _visible(page, 'input[type="password"], input[name="current-password"]'):
            if has_mail and not otp_sent and _click_one_time_code(page):
                otp_sent = True
                emit("email_otp", "密码页改走邮箱一次性验证码")
                page.wait_for_timeout(2500)
                continue
            body = page_text.lower()
            if password and not password_tried:
                emit("password", "正在填写密码")
                _fill_first(
                    page,
                    ['input[name="current-password"]', 'input[type="password"]', 'input[name="password"]'],
                    password,
                )
                _click_exact(page, ["Continue"]) or _click_first(page, ['button[type="submit"]'])
                password_tried = True
                page.wait_for_timeout(2500)
                continue
            if not password:
                result["error"] = "登录页要密码，但子号没有保存密码，也没有邮箱验证码入口"
                result["error_code"] = "password_missing"
                break
            if require_session_match and password_tried and any(token in body for token in _PASSWORD_ERRORS):
                result["error"] = "密码不对，也没有邮箱验证码入口"
                result["error_code"] = "password_rejected"
                break
            page.wait_for_timeout(1200)
            continue

        if _visible(page, 'input[type="tel"]') or "add-phone" in url or "phone-verification" in url:
            if not allow_sms:
                result["error"] = (
                    "本次流程不自动接短信，页面要求手机验证，已停止"
                    if require_session_match
                    else "邀请注册要求手机验证，请人工处理后继续同一邮箱"
                )
                result["error_code"] = "phone_verification_required"
                break
            emit("add_phone", "页面要求添加手机号")
            outcome, msg = phone_page_outcome(page, allow_risk=False)
            if outcome in {"invalid", "recently_used", "risk"}:
                if phone_source:
                    record_pool_phone(phone_source, outcome, msg)
                    emit("add_phone", f"换号原因：{msg}")
                    number, next_sms_url, error, code = take_pool_phone(phone_source, emit)
                    if error:
                        result["error"] = error
                        result["error_code"] = code
                        break
                    phone, sms_url = number, next_sms_url
                    continue
                result["error"] = msg or "手机号不被 OpenAI 接受。Codex 授权通常不吃 +86，要换能过的接码号"
                result["error_code"] = "sms_rejected"
                break
            if not phone or not sms_url:
                if phone_source:
                    number, next_sms_url, error, code = take_pool_phone(phone_source, emit)
                    if error:
                        result["error"] = error
                        result["error_code"] = code
                        break
                    phone, sms_url = number, next_sms_url
                elif _click_exact(page, ["Skip", "Not now", "Maybe later", "Skip for now", "I'll do this later"]):
                    emit("add_phone", "注册页出现手机号，已跳过")
                    page.wait_for_timeout(1500)
                    continue
                else:
                    result["error"] = "注册页要手机号。正常邀请注册通常不需要，Codex 授权时才接码"
                    result["error_code"] = "sms_missing"
                    break
            if not phone or not sms_url:
                break
            _fill_phone_number(page, phone)
            _submit_phone_sms(page)
            page.wait_for_timeout(2500)
            outcome, msg = phone_page_outcome(page)
            if outcome in {"invalid", "recently_used", "risk"}:
                if phone_source:
                    record_pool_phone(phone_source, outcome, msg)
                    emit("add_phone", f"换号原因：{msg}")
                    number, next_sms_url, error, code = take_pool_phone(phone_source, emit)
                    if error:
                        result["error"] = error
                        result["error_code"] = code
                        break
                    phone, sms_url = number, next_sms_url
                    continue
                result["error"] = msg or "手机号不被 OpenAI 接受。Codex 授权通常不吃 +86，要换能过的接码号"
                result["error_code"] = "sms_rejected"
                break
            emit("sms_otp", "等待短信验证码")
            try:
                sms_code = sms_client.wait_for_code(sms_url, proxy=proxy, timeout_sec=90)
            except TimeoutError as exc:
                msg = str(exc) or "接码超时或空号"
                if phone_source:
                    record_pool_phone(phone_source, "no_sms", msg)
                    emit("add_phone", f"换号原因：{msg}")
                    number, next_sms_url, error, code = take_pool_phone(phone_source, emit)
                    if error:
                        result["error"] = error
                        result["error_code"] = code or "sms_failed"
                        break
                    phone, sms_url = number, next_sms_url
                    continue
                result["error"] = msg
                result["error_code"] = "sms_failed"
                break
            otp_el = _find_otp(page)
            if otp_el:
                started_url = page.url or ""
                otp_el.fill(sms_code)
                result["sms_code_submissions"] = int(result["sms_code_submissions"]) + 1
                _wait_and_fallback_submit_otp(
                    page,
                    started_url=started_url,
                    exact_names=["Continue", "Verify"],
                    selectors=['button[type="submit"]'],
                )
            else:
                _click_exact(page, ["Continue", "Verify"]) or _click_first(page, ['button[type="submit"]'])
            page.wait_for_timeout(2500)
            record_pool_phone(phone_source, "success")
            result["phone"] = phone
            result["sms_url"] = sms_url
            continue

        if _visible(page, 'input[name="name"], input[name="fullName"], input[name="firstName"]'):
            about_you_tries += 1
            if about_you_tries > ABOUT_YOU_STUCK_LIMIT:
                result["error"] = "卡在年龄/生日页，没有进入下一步"
                result["error_code"] = "about_you_stuck"
                break
            emit("profile", "正在填写资料")
            _fill_about_you(page)
            page.wait_for_timeout(2500)
            continue

        if _accept_terms(page):
            emit("terms", "已勾选服务条款")
            page.wait_for_timeout(1500)
            continue

        if invite_entry and _pick_workspace(page, team_name):
            emit("workspace", "正在接受邀请并选择目标工作空间")
            page.wait_for_timeout(1500)
            continue

        peeked = _peek_session(page)
        if session_access_token(peeked):
            if require_session_match:
                actual = session_email(peeked)
                if actual == wanted_email:
                    session = peeked
                    break
                if actual:
                    result["error"] = "ChatGPT 登录态邮箱与目标邮箱不一致，未自动退出"
                    result["error_code"] = "identity_mismatch"
                    break
            else:
                if invite_entry and not invite_reopened:
                    if session_email(peeked) != wanted_email:
                        return {
                            **result,
                            "ok": False,
                            "phone": phone,
                            "sms_url": sms_url,
                            "error_code": "token_identity_mismatch",
                            "error": "登录态邮箱与邀请不一致",
                        }
                    invite_reopened = True
                    goto_with_retries(page, start_url, report=report)
                    page.wait_for_timeout(1500)
                    continue
                session = peeked
                break

        polled = {} if invite_entry else _poll_session(page)
        if session_access_token(polled):
            if require_session_match:
                actual = session_email(polled)
                if actual == wanted_email:
                    session = polled
                    break
                if actual:
                    result["error"] = "ChatGPT 登录态邮箱与目标邮箱不一致，未自动退出"
                    result["error_code"] = "identity_mismatch"
                    break
            else:
                session = polled
                break

        credentials_submitted = otp_submits > 0 or password_tried or about_you_tries > 0
        if require_session_match and credentials_submitted and can_peek_session(url) and "/auth/login" not in url:
            waited = wait_chatgpt_session(
                page,
                wanted_email,
                timeout_sec=session_wait_sec,
                report=report,
                break_on_auth_form=True,
            )
            status = waited.get("status")
            if status == "ok":
                session = waited["session"]
                break
            if status == "navigated_away":
                continue
            result["error"] = waited.get("error") or "ChatGPT 会话未就绪"
            result["error_code"] = waited.get("error_code") or "registration_home_not_ready"
            break

        title = page_title.lower()
        if require_session_match and can_peek_session(url) and not credentials_submitted:
            if login_clicks < 2 and _click_login_entry(page):
                login_clicks += 1
                emit("chatgpt_login", "未登录首页，点 Log in")
                page.wait_for_timeout(2000)
                continue
            if "/auth/login" not in url:
                login_clicks += 1
                goto_with_retries(page, LOGIN_START_URL, report=report)
                page.wait_for_timeout(2000)
                continue
        elif "chat, work, create" in title or "where should we begin" in page_text.lower():
            if mode == "register" and (
                _click_exact(page, ["Sign up for free", "Sign up"])
                or _click_first(page, ['a:has-text("Sign up")', 'button:has-text("Sign up")'])
            ):
                emit("signup", "未登录首页，点 Sign up")
                page.wait_for_timeout(2000)
                continue
            if _click_exact(page, ["Log in"]):
                emit("login", "未登录首页，点 Log in 回去")
            page.wait_for_timeout(2000)
            continue

        if "auth.openai.com" in url and (
            _visible(page, 'input[type="password"], input[name="current-password"]')
            or bool(_find_otp(page) or _otp_boxes(page))
            or "email-verification" in url
            or looks_like_about_you(title=page_title, body=page_text, url=url)
        ):
            page.wait_for_timeout(1200)
            continue

        if not require_session_match:
            if _pick_workspace(page, team_name):
                emit("workspace", "已选择工作空间")
                page.wait_for_timeout(1500)
                continue
            if _click_exact(page, ["Accept", "I agree", "Okay", "Next"]):
                page.wait_for_timeout(1500)
                continue
        page.wait_for_timeout(1200)

    result["phone"] = phone
    result["sms_url"] = sms_url
    if require_session_match:
        if result.get("error_code") and not session_access_token(session):
            result["final_url"] = page.url or ""
            return result
        if not session_access_token(session):
            waited = wait_chatgpt_session(page, wanted_email, timeout_sec=session_wait_sec, report=report)
            if waited.get("status") == "ok":
                session = waited["session"]
            else:
                result["error"] = waited.get("error") or "登录后 ChatGPT 会话未就绪"
                result["error_code"] = waited.get("error_code") or "registration_home_not_ready"
                result["final_url"] = page.url or ""
                return result
        actual = session_email(session)
        if actual != wanted_email:
            result["error"] = "ChatGPT 登录态邮箱与目标邮箱不一致，未自动退出"
            result["error_code"] = "identity_mismatch"
            result["final_url"] = page.url or ""
            return result
        js = session.get("json") if isinstance(session.get("json"), dict) else {}
        user = session_user(session)
        result.update({
            "ok": True,
            "skipped_login": False,
            "access_token": session_access_token(session),
            "session_token": str((js or {}).get("sessionToken") or "").strip(),
            "account_id": str((user or {}).get("id") or ""),
            "final_url": page.url or "",
        })
        return result

    if profile_dir is not None:
        result["debug_dir"] = _save_debug(page, profile_dir)
    if result.get("error_code") and not session_access_token(session):
        result["final_url"] = page.url or ""
        return result
    emit("session", f"正在读取登录态 {page.url or ''}")
    if not session_access_token(session):
        session = _extract_session(page)
    access_token = session_access_token(session)
    js = session.get("json") if isinstance(session.get("json"), dict) else {}
    session_token = str((js or {}).get("sessionToken") or "").strip()
    user = session_user(session)
    if invite_entry and session_email(session) != wanted_email:
        return {
            **result,
            "ok": False,
            "error_code": "token_identity_mismatch",
            "error": "邀请注册登录态邮箱不匹配",
        }
    result.update({
        "ok": bool(access_token),
        "access_token": access_token,
        "session_token": session_token,
        "account_id": str((user or {}).get("id") or ""),
        "final_url": page.url or "",
    })
    if not access_token:
        if profile_dir is not None:
            result["debug_dir"] = _save_debug(page, profile_dir)
        if looks_like_email_gate(title=page.title() or "", body=_page_text(page), url=page.url or "") or _email_input_visible(page):
            result["error"] = result.get("error") or "卡在 ChatGPT 邮箱页，没有进入 OpenAI 注册"
            result["error_code"] = result.get("error_code") or "email_gate_stuck"
        else:
            result["error"] = result.get("error") or _session_failure(page, session)
            result["error_code"] = result.get("error_code") or (
                "cloudflare_challenge" if page_is_cloudflare(page) else "session_missing"
            )
    return result


def ensure_chatgpt_login(
    page,
    *,
    email: str,
    password: str = "",
    pickup_url: str = "",
    proxy: str = "",
    use_cloudflare: bool = False,
    cf_base_url: str = "",
    cf_address: str = "",
    cf_admin_password: str = "",
    hme_base_url: str = "",
    hme_service_token: str = "",
    hme_account_id: str = "",
    allow_profile_setup: bool = False,
    allow_sms: bool = False,
    phone: str = "",
    sms_url: str = "",
    phone_source=None,
    on_stage: StageCallback = None,
    session_wait_sec: float = 30.0,
) -> dict[str, Any]:
    """Ensure chatgpt.com has a live session for ``email`` before opening desktop-auth.

    1. Open https://chatgpt.com/ if not already on a chatgpt.com page; wait Cloudflare.
    2. Check /api/auth/session: matching email -> skip login; other email -> identity_mismatch (never logout).
    3. Log in with email OTP (or password if provided and no OTP entry); optionally fill about-you when allowed.
    4. Poll /api/auth/session (up to 30s) until accessToken + target email are confirmed.
    """
    wanted_email = str(email or "").strip().lower()
    if not can_peek_session(page.url or ""):
        if on_stage:
            on_stage("browser_open", "正在打开 ChatGPT 确认登录状态")
        goto_with_retries(page, CHATGPT_HOME_URL, report=on_stage)
    if not wait_cloudflare(page):
        return {
            "ok": False,
            "error_code": "cloudflare_challenge",
            "error": _session_failure(page, {"status": "cloudflare"}),
            "phone": phone,
            "sms_url": sms_url,
            "sms_code_submissions": 0,
        }
    page.wait_for_timeout(1200)

    existing = peek_chatgpt_session(page)
    if session_access_token(existing):
        actual = session_email(existing)
        if actual == wanted_email:
            if on_stage:
                on_stage("chatgpt_session", "ChatGPT 已登录目标邮箱，跳过登录")
            js = existing.get("json") if isinstance(existing.get("json"), dict) else {}
            user = session_user(existing)
            return {
                "ok": True,
                "skipped_login": True,
                "access_token": session_access_token(existing),
                "session_token": str((js or {}).get("sessionToken") or "").strip(),
                "account_id": str((user or {}).get("id") or ""),
                "phone": phone,
                "sms_url": sms_url,
                "sms_code_submissions": 0,
            }
        return {
            "ok": False,
            "error_code": "identity_mismatch",
            "error": "ChatGPT 当前已登录其他账号，与目标邮箱不一致，未自动退出",
            "phone": phone,
            "sms_url": sms_url,
            "sms_code_submissions": 0,
        }

    return run_chatgpt_login_loop(
        page,
        email=email,
        password=password,
        pickup_url=pickup_url,
        phone=phone,
        sms_url=sms_url,
        proxy=proxy,
        start_url=CHATGPT_HOME_URL,
        mode="login",
        use_cloudflare=use_cloudflare,
        cf_base_url=cf_base_url,
        cf_address=cf_address,
        cf_admin_password=cf_admin_password,
        hme_base_url=hme_base_url,
        hme_service_token=hme_service_token,
        hme_account_id=hme_account_id,
        phone_source=phone_source,
        allow_sms=allow_sms,
        allow_profile_setup=allow_profile_setup,
        invite_entry=False,
        require_session_match=True,
        initial_goto=False,
        session_wait_sec=session_wait_sec,
        report=on_stage,
    )
