"""Run against tests.preview_app on localhost:8019; never a production URL."""
import json
from pathlib import Path
from tempfile import gettempdir
from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8019"
OUT = Path(gettempdir()) / "team48-browser"
OUT.mkdir(exist_ok=True)


def main():
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        login = context.request.post(BASE + "/auth/login", data={"username": "preview", "password": "preview-only"})
        assert login.ok, login.text()
        page = context.new_page()
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(BASE + "/accounts")
        page.wait_for_selector(".management-table tbody tr")
        assert page.locator("#summary-accounts").inner_text() == "7"
        assert page.locator("#summary-needs_auth").inner_text() == "2"
        assert page.locator("#summary-retry").inner_text() == "1"
        assert "$12.35" in page.locator("#accounts-portfolio").inner_text()
        for width in (1600, 1440, 1280, 1024, 768, 390):
            page.set_viewport_size({"width": width, "height": 1000 if width > 540 else 844})
            page.screenshot(path=str(OUT / f"accounts-{width}.png"), full_page=True, animations="disabled")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            assert page.locator('#page-root').evaluate('n => n.getBoundingClientRect().right <= innerWidth'), width
            assert page.locator('.management-summary').evaluate('n => n.getBoundingClientRect().right <= innerWidth'), width
            overlaps = page.locator(".management-actions").evaluate_all("""nodes => nodes.filter(n => {
                const [a,b] = n.children; if (!a || !b) return false;
                return a.getBoundingClientRect().right > b.getBoundingClientRect().left;
            }).length""")
            assert overlaps == 0, (width, overlaps)
        page.set_viewport_size({"width": 1440, "height": 1000})
        page.locator("#accounts-search").fill("member.research")
        assert page.locator(".management-table tbody tr").count() == 1
        assert "North" in page.locator(".management-group-name").inner_text()
        page.locator(".management-email").click()
        assert page.locator(".sheet:not([hidden])").count() == 1
        assert "401" in page.locator("#account-health-content").inner_text()
        assert "旧快照" in page.locator("#account-health-content").inner_text()
        page.locator(".management-back").click()
        assert page.locator(".sheet:not([hidden])").count() == 1
        assert "North" in page.locator("#sheet-title").inner_text()
        page.keyboard.press("Escape")
        page.locator("#accounts-search").fill("")
        page.locator("#management-workspace").select_option("")
        page.locator("#management-health").select_option("401")
        assert page.locator(".management-table tbody tr").count() == 1
        page.locator("#management-health").select_option("all")
        page.locator('.management-tabs [data-management-view="all"]').click()
        assert page.locator(".management-table tbody tr").count() == 7
        page.locator('.management-tabs [data-management-view="unassigned"]').click()
        assert page.locator(".management-table tbody tr").count() == 1
        assert page.locator('.management-bar[role="meter"]').count() == 0
        page.locator('.management-tabs [data-management-view="teams"]').click()
        toggle = page.locator('.management-expand').first
        toggle.click()
        assert toggle.get_attribute("aria-expanded") == "false"
        page.evaluate("window.Team48.bootPage()")
        assert page.locator('.management-expand').first.get_attribute("aria-expanded") == "false"
        page.locator('.management-expand').first.click()
        more = page.locator('.management-actions [data-menu-trigger]').first
        more.click()
        assert page.locator("#action-menu").is_visible()
        page.keyboard.press("ArrowDown")
        page.keyboard.press("Escape")
        assert not page.locator("#action-menu").is_visible()
        assert page.evaluate("document.activeElement.hasAttribute('data-menu-trigger')")
        page.get_by_role('button', name='登记账号或团队').click()
        page.get_by_role('menuitem', name='登记账号', exact=True).click()
        assert page.locator('.sheet:not([hidden])').count() == 1
        page.locator('#sheet-body input[name="email"]').fill('preview-new@example.com')
        page.locator('#sheet-body button[type="submit"]').click()
        page.wait_for_function("document.querySelector('#sheet-body [role=alert]').textContent.includes('隔离预览')")
        page.keyboard.press('Escape')
        page.goto(BASE + "/workspaces?workspace=1&account=2")
        page.wait_for_selector("#account-health-content")
        assert "member.research" in page.locator("#sheet-title").inner_text()
        assert "/accounts?" in page.url
        page.screenshot(path=str(OUT / "account-detail.png"), full_page=True, animations="disabled")
        assert not errors, errors
        print(json.dumps({"screenshots": str(OUT), "widths": [1600, 1440, 1280, 1024, 768, 390], "page_errors": errors,
                          "checks": "summary, money, filtering, views, sheet/back, unknown quota, collapse refresh, keyboard menu, deep links"}))
        browser.close()


if __name__ == "__main__":
    main()
