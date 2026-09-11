"""Isolated preview only. Every Codex/settings request is intercepted."""
import json
import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = os.environ.get("TEAM48_PREVIEW_URL", "http://127.0.0.1:8029")
settings = {"connections": {"codex_base_url": "https://codex.example", "codex": {"configured": True}},
            "secrets": {"codex_admin_key": ""}, "secret_state": {"codex_admin_key": "stored"},
            "automation": {}, "resources": {}, "account": {"username": "preview"}}
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={"width": 1440, "height": 1000}, accept_downloads=True)
    assert context.request.post(BASE + "/auth/login", data={"username": "preview", "password": "preview-only"}).ok
    portfolio = context.request.get(BASE + "/api/accounts/portfolio").json()
    selected = [a for a in portfolio["accounts"] if a.get("id")][:2]
    calls, errors = [], []

    def intercept(route):
        path = route.request.url.split("?", 1)[0]
        if path.endswith("/api/settings"):
            if route.request.method != "GET":
                calls.append(("settings", route.request.post_data_json))
            return route.fulfill(json=settings)
        if path.endswith("/api/accounts/codex/status"):
            return route.fulfill(json={"items": []})
        if path.endswith("/api/accounts/codex/export"):
            body = route.request.post_data_json
            calls.append(("export", body))
            return route.fulfill(headers={"Content-Type": "application/json", "Content-Disposition": 'attachment; filename="team48-codex-at-only.json"'},
                body=json.dumps({"accounts": [{"access_token": "preview-at", "id_token": "preview-id"}]}))
        if path.endswith("/api/accounts/codex/push"):
            raise AssertionError("Removed Codex button must not send a push")
        if route.request.method not in {"GET", "HEAD"}:
            raise AssertionError("Unexpected write: " + route.request.url)
        route.continue_()

    context.route("**/api/**", intercept)
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(BASE + "/accounts?view=all")
    for item in selected:
        page.get_by_label("选择 " + item["email"], exact=True).check()
    page.locator("#account-selection-export").click()
    page.get_by_role("dialog", name="导出 Codex 凭据").wait_for()
    page.get_by_role("button", name="取消", exact=True).first.click()
    assert not calls
    page.locator("#account-selection-export").click()
    with page.expect_download() as info:
        page.get_by_role("button", name="确认导出", exact=True).click()
    download = info.value
    assert download.suggested_filename == "team48-codex-at-only.json"
    assert "refresh_token" not in Path(download.path()).read_text()
    assert calls[0][1] == {"confirm": True, "account_ids": [a["id"] for a in selected]}
    out = Path(tempfile.gettempdir()) / "team48-codex-browser"
    out.mkdir(exist_ok=True)
    for width, height in ((1440, 1000), (390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        bar = page.locator("#account-selection-bar")
        bar.scroll_into_view_if_needed()
        assert page.locator("#account-selection-push").count() == 0
        for name in ("account-selection-export", "account-selection-clear", "account-selection-delete"):
            assert page.locator("#" + name).evaluate("n => { const r=n.getBoundingClientRect(); return r.left >= 0 && r.right <= innerWidth && n.scrollWidth <= n.clientWidth; }")
        page.screenshot(path=str(out / f"accounts-{width}.png"))
    assert all(kind != "push" for kind, _ in calls)
    page.goto(BASE + "/settings")
    page.locator("input[name=codex_base_url]").wait_for()
    assert page.locator("input[name=codex_admin_key]").input_value() == ""
    assert page.locator("input[name=codex_base_url]").input_value() == "https://codex.example"
    for width, height in ((1440, 1000), (390, 844)):
        page.set_viewport_size({"width": width, "height": height})
        page.locator("[data-service=codex]").scroll_into_view_if_needed()
        assert page.locator("input[name=codex_base_url]").evaluate("n => {const r=n.getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth;}")
        page.screenshot(path=str(out / f"settings-{width}.png"))
    assert not errors, errors
    print(json.dumps({"checks": "selection, cancel, download, push button absent, secret field, desktop/mobile", "screenshots": str(out), "page_errors": errors}))
    browser.close()
