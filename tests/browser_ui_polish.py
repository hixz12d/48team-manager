"""UI polish checks against tests.preview_app on localhost:8019. Resource rows are stubbed locally."""
import json
import os
from pathlib import Path
from tempfile import gettempdir

from playwright.sync_api import sync_playwright

BASE = os.environ.get("TEAM48_PREVIEW_URL", "http://127.0.0.1:8019")
OUT = Path(gettempdir()) / "team48-browser"
OUT.mkdir(exist_ok=True)

PHONES = {"items": [
    {"id": index, "number": f"+1555000{index:04d}", "status": "active" if index % 2 else "disabled",
     "used_count": (index * 7) % 5, "max_uses": 5, "risk_count": (index * 3) % 4,
     "reserved_by": None, "cooldown_until": f"2026-09-{(index % 27) + 1:02d}T10:00:00+00:00",
     "last_error_type": None}
    for index in range(1, 31)
]}


def scroll_down(page, amount):
    page.evaluate(
        "amount => { const page = document.querySelector('.page');"
        " if (page && page.scrollHeight > page.clientHeight) page.scrollBy(0, amount); else window.scrollBy(0, amount); }",
        amount,
    )
    page.wait_for_timeout(120)


def main():
    errors = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1600, "height": 700})
        assert context.request.post(BASE + "/auth/login", data={"username": "preview", "password": "preview-only"}).ok
        failed_patch = {"count": 0}
        resource_reads = []

        def phones_route(route):
            if route.request.method == "PATCH":
                failed_patch["count"] += 1
                return route.fulfill(status=500, json={"detail": {"message": "预览环境不写入手机号"}})
            resource_reads.append(route.request.url)
            return route.fulfill(json=PHONES)

        context.route("**/api/resources/phones", phones_route)
        context.route("**/api/resources/phones/*", phones_route)
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: errors.append(f"native dialog: {dialog.message}"))

        # 1. Command palette is keyboard operable.
        page.goto(BASE + "/resources/phones")
        page.wait_for_selector("#phones-body tr td")
        page.keyboard.press("Control+k")
        page.wait_for_selector("#command-palette[open] #command-list li button")
        assert page.locator("#command-list li button").count() >= 2
        page.keyboard.press("ArrowDown")
        assert page.locator("#command-list li").nth(1).get_attribute("class") == "is-active"
        page.keyboard.press("ArrowUp")
        assert page.locator("#command-list li").first.get_attribute("class") == "is-active"
        page.locator("#command-input").fill("no-such-destination")
        assert page.locator(".palette-empty").is_visible()
        page.keyboard.press("Escape")
        page.keyboard.press("Control+k")
        assert page.locator("#command-input").input_value() == ""
        page.keyboard.type("账号")
        page.wait_for_function("document.querySelectorAll('#command-list li[data-href]').length === 1")
        assert page.locator("#command-list li.is-active").count() == 1
        page.keyboard.press("Enter")
        page.wait_for_url("**/accounts")

        # 2. Account filters show removable chips and a persistent clear control.
        page.wait_for_selector(".management-table tbody tr")
        assert page.locator("#accounts-clear-filters").is_hidden()
        page.locator("#accounts-search").fill("all")
        assert "q=all" in page.url
        assert page.locator("#accounts-active-filters .management-chip").count() == 1
        page.locator("#accounts-clear-filters").click()
        page.locator("#accounts-search").fill("member")
        page.wait_for_selector("#accounts-active-filters .management-chip")
        assert page.locator("#accounts-clear-filters").is_visible()
        page.locator("#management-health").select_option("auth")
        assert page.locator("#accounts-active-filters .management-chip").count() == 2
        page.locator("#accounts-active-filters .management-chip-clear").first.click()
        page.wait_for_function("document.querySelectorAll('#accounts-active-filters .management-chip').length === 1")
        assert page.locator("#accounts-search").input_value() == ""
        page.locator("#accounts-clear-filters").click()
        page.wait_for_selector("#accounts-active-filters", state="hidden")
        assert page.locator("#management-health").input_value() == "all"
        assert "health=" not in page.url and "q=" not in page.url

        # 3. Account columns sort the whole filtered set and keep an accessible state.
        teams_emails = page.locator(".management-group").first.locator("tbody .management-email").all_inner_texts()
        page.locator('.management-table thead [data-focus-key="sort:email"]').first.click()
        page.wait_for_function("!!document.querySelector('.management-table th[aria-sort=\"ascending\"]')")
        sorted_in_team = page.locator(".management-group").first.locator("tbody .management-email").all_inner_texts()
        assert sorted_in_team == sorted(teams_emails), (teams_emails, sorted_in_team)
        page.locator('.management-tabs [data-management-view="all"]').click()
        page.wait_for_selector(".management-table tbody tr")
        page.wait_for_function("!!document.querySelector('.management-table th[aria-sort=\"ascending\"]')")
        assert "sort=email" in page.url
        emails = page.locator(".management-table tbody .management-email").all_inner_texts()
        assert emails == sorted(emails), emails
        page.locator('.management-table thead [data-focus-key="sort:email"]').first.click()
        page.wait_for_function("!!document.querySelector('.management-table th[aria-sort=\"descending\"]')")
        descending = page.locator(".management-table tbody .management-email").all_inner_texts()
        assert descending == sorted(descending, reverse=True), descending
        page.locator('.management-table thead [data-focus-key="sort:email"]').first.click()
        page.wait_for_function("!document.querySelector('.management-table th[aria-sort=\"descending\"]')")
        assert "sort=" not in page.url

        # 4. Team table headers stay visible while scrolling long teams.
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(150)
        header = page.locator(".management-table thead th").first
        body_row = page.locator(".management-table tbody tr").first
        before_header = header.bounding_box()["y"]
        before_row = body_row.bounding_box()["y"]
        scroll_down(page, before_header + 80)
        after_header = header.bounding_box()["y"]
        after_row = body_row.bounding_box()["y"]
        assert after_row < before_row - 80, (before_row, after_row)
        assert -1 <= after_header <= 12, (before_header, after_header)
        page.screenshot(path=str(OUT / "sticky-accounts-1600.png"))

        # 5. Resource tables sort on click and keep their header pinned.
        page.goto(BASE + "/resources/phones")
        page.wait_for_selector("#phones-body tr td")
        numbers = page.locator("#phones-body tr td:first-child").all_inner_texts()
        page.locator('th[data-sort="number"] .table-sort').click()
        page.wait_for_selector('th[data-sort="number"][aria-sort="ascending"]')
        assert page.locator("#phones-body tr td:first-child").all_inner_texts() == sorted(numbers)
        page.locator('th[data-sort="number"] .table-sort').click()
        page.wait_for_selector('th[data-sort="number"][aria-sort="descending"]')
        assert page.locator("#phones-body tr td:first-child").all_inner_texts() == sorted(numbers, reverse=True)
        page.locator('th[data-sort="uses"] .table-sort').click()
        page.wait_for_selector('th[data-sort="uses"][aria-sort="ascending"]')
        used = [int(text.split("/")[0]) for text in page.locator("#phones-body tr td:nth-child(3)").all_inner_texts()]
        assert used == sorted(used), used
        header = page.locator(".data-table thead th").first
        row = page.locator("#phones-body tr").first
        before_header = header.bounding_box()["y"]
        before_row = row.bounding_box()["y"]
        scroll_down(page, before_header + 80)
        after_header = header.bounding_box()["y"]
        assert row.bounding_box()["y"] < before_row - 80
        assert -1 <= after_header <= 12, (before_header, after_header)
        page.screenshot(path=str(OUT / "sticky-phones-1600.png"))

        # 6. Dangerous actions use the in-app confirmation, and failures stay until dismissed.
        assert len(resource_reads) == 2, "sorting must reuse the resource snapshot"
        page.evaluate("window.scrollTo(0, 0)")
        page.locator("#phones-body tr").first.get_by_role("button", name="停用").click()
        page.wait_for_selector("#confirm-sheet:not([hidden])")
        assert "停用手机号" in page.locator("#confirm-title").inner_text()
        page.locator("#confirm-sheet .confirm-actions [data-close-confirm]").click()
        page.wait_for_selector("#confirm-sheet", state="hidden")
        assert failed_patch["count"] == 0
        page.locator("#phones-body tr").first.get_by_role("button", name="停用").click()
        page.wait_for_selector("#confirm-sheet:not([hidden])")
        page.locator("#confirm-submit").click()
        page.wait_for_selector(".toast-error")
        assert failed_patch["count"] == 1
        page.wait_for_timeout(7000)
        assert page.locator(".toast-error").count() == 1, "failure notice disappeared on its own"
        page.locator(".toast-error .toast-close").click()
        page.wait_for_selector(".toast-error", state="detached")

        # Narrow tables must retain access to their rightmost columns.
        for path, scroller in [("/resources/phones", ".table-scroll"), ("/accounts?view=all", ".management-table-scroll")]:
            page.goto(BASE + path)
            page.wait_for_selector(scroller + " tbody tr")
            for width in (1400, 1280, 1120, 768, 390):
                page.set_viewport_size({"width": width, "height": 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (path, width)
                box = page.locator(scroller).first
                box.evaluate("node => node.scrollLeft = node.scrollWidth")
                assert box.locator("th").last.evaluate("node => { const r = node.getBoundingClientRect(); return r.right <= innerWidth + 1; }"), (path, width)
                if width == 390:
                    page.screenshot(path=str(OUT / ("phones-mobile.png" if "phones" in path else "accounts-mobile.png")), animations="disabled")

        assert not errors, errors
        print(json.dumps({
            "checks": "palette keyboard, filter chips + clear, account sorting, sticky headers, resource sorting, in-app confirm, sticky failure toast",
            "viewport": [1600, 700], "page_errors": errors, "screenshots": str(OUT),
        }, ensure_ascii=False))
        browser.close()


if __name__ == "__main__":
    main()
