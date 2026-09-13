"""Browser checks reuse the isolated local app and lifecycle from the expiry preview."""
from datetime import datetime, timedelta

from tests import browser_workspace_expiry as preview


def check(base, output):
    from playwright.sync_api import expect, sync_playwright

    errors, writes = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, timezone_id="America/Los_Angeles")
        assert context.request.post(base + "/auth/login", data={"username": "preview", "password": "preview-only"}).ok
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: writes.append(request.url) if request.method == "POST" else None)
        page.goto(base + "/accounts")
        counter = page.locator('[data-switch-workspace="1"]')
        label = counter.get_by_role("status")
        button = counter.get_by_role("button")
        expect(label).to_have_text("今日切换 0 次")
        button.click()
        expect(label).to_have_text("今日切换 1 次")
        button.click()
        expect(label).to_have_text("今日切换 2 次")
        expect(page.locator('[data-switch-workspace="2"] [role="status"]')).to_have_text("今日切换 0 次")
        page.reload()
        expect(label).to_have_text("今日切换 2 次")
        # Failures must leave the displayed count intact and permit a deliberate retry.
        page.route("**/api/workspaces/1/switch-count/increment", lambda route: route.fulfill(status=503, json={"detail": "测试网络故障"}), times=1)
        button.click()
        expect(page.get_by_role("alert")).to_contain_text("计数未确认")
        expect(label).to_have_text("今日切换 2 次")
        expect(button).to_be_enabled()
        button.click()
        expect(label).to_have_text("今日切换 3 次")
        # A pending click remains guarded even if polling rebuilds the team card.
        captured = []
        endpoint = "**/api/workspaces/1/switch-count/increment"
        page.route(endpoint, lambda route: captured.append(route))
        before = len(writes)
        button.click()
        expect(button).to_be_disabled()
        page.wait_for_function("document.querySelector('[data-switch-workspace=\"1\"] button').disabled")
        stale = context.request.get(base + "/api/accounts/portfolio").json()
        stale["groups"][0]["display_name"] = "North · 研究团队（刷新）"
        page.evaluate("data => Team48Accounts.render(data)", stale)
        expect(button).to_be_disabled()
        button.evaluate("b => b.dispatchEvent(new MouseEvent('click', {bubbles: true}))")
        assert len(writes) == before + 1
        assert len(captured) == 1
        captured[0].continue_()
        page.unroute(endpoint)
        expect(label).to_have_text("今日切换 4 次")
        expect(button).to_be_enabled()
        page.evaluate("data => Team48Accounts.render(data)", stale)
        expect(label).to_have_text("今日切换 4 次")
        # A second browser tab sees the durable record and its updates are read back.
        second = context.new_page()
        second.goto(base + "/accounts")
        second_counter = second.locator('[data-switch-workspace="1"]')
        expect(second_counter.get_by_role("status")).to_have_text("今日切换 4 次")
        second_counter.get_by_role("button").click()
        expect(second_counter.get_by_role("status")).to_have_text("今日切换 5 次")
        page.evaluate("Team48Accounts.refresh()")
        expect(label).to_have_text("今日切换 5 次")
        second.close()
        # Counting also works while the member table is collapsed, without toggling it.
        toggle = page.locator('[data-focus-key="expand:1"]')
        toggle.click()
        expect(toggle).to_have_attribute("aria-expanded", "false")
        button.focus()
        page.keyboard.press("Enter")
        expect(label).to_have_text("今日切换 6 次")
        expect(toggle).to_have_attribute("aria-expanded", "false")
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 1000 if width > 540 else 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            bounds = counter.bounding_box()
            assert bounds and bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width, (width, bounds)
            page.screenshot(path=str(output / f"switch-count-{width}.png"), full_page=True)
        # Freeze browser time immediately before Beijing midnight; API/polling is stopped
        # so that the timer itself, rather than a server refresh, must clear the label.
        data = context.request.get(base + "/api/accounts/portfolio").json()
        day = data["groups"][0]["switch_count"]["date"]
        before_midnight = datetime.fromisoformat(day + "T23:59:58+08:00")
        page.clock.install(time=before_midnight)
        page.clock.pause_at(before_midnight)
        page.reload()
        expect(label).to_have_text("今日切换 6 次")
        page.route("**/api/accounts/portfolio", lambda route: route.abort())
        page.clock.run_for(2100)
        expect(label).to_have_text("今日切换 0 次")
        next_day = (before_midnight + timedelta(days=1)).date().isoformat()
        page.route(endpoint, lambda route: route.fulfill(json={"ok": True, "workspace_id": 1,
            "switch_count": {"date": next_day, "count": 1, "timezone": "Asia/Shanghai"}}))
        button.click()
        expect(label).to_have_text("今日切换 1 次")
        assert not errors, errors
        browser.close()
    return {"screenshots": str(output), "widths": [1440, 768, 390], "page_errors": errors,
            "checks": "increment, persistence, separate teams, retry, pending duplicate guard, stale polling, two tabs, keyboard, collapsed team, Beijing midnight timer, responsive layout"}


if __name__ == "__main__":
    preview.check = check
    preview.main()
