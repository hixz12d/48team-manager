"""Actual Chromium tab zoom, not deviceScaleFactor or CSS zoom."""
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    extension = Path(__file__).parent / "browser_zoom"
    with tempfile.TemporaryDirectory() as profile, sync_playwright() as p:
        context = p.chromium.launch_persistent_context(profile, headless=True, channel="chromium",
            viewport={"width": 1440, "height": 1000},
            args=[f"--disable-extensions-except={extension.resolve()}", f"--load-extension={extension.resolve()}"])
        worker = context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
        context.request.post("http://127.0.0.1:8019/auth/login", data={"username": "preview", "password": "preview-only"})
        page = context.pages[0]
        page.goto("http://127.0.0.1:8019/accounts")
        page.wait_for_selector(".management-table tbody tr")
        zoom = worker.evaluate("""async () => {
            const tabs = await chrome.tabs.query({url:'http://127.0.0.1:8019/*'});
            await chrome.tabs.setZoom(tabs[0].id, 2);
            return await chrome.tabs.getZoom(tabs[0].id);
        }""")
        assert zoom == 2
        page.wait_for_timeout(300)
        assert page.locator('#page-root').evaluate('n => n.getBoundingClientRect().right <= innerWidth')
        assert page.locator('.management-summary').evaluate('n => n.getBoundingClientRect().right <= innerWidth')
        path = Path(tempfile.gettempdir()) / "team48-browser" / "accounts-zoom-200.png"
        page.screenshot(path=str(path), full_page=True, animations="disabled")
        print({"actual_tab_zoom": zoom, "inner_width": page.evaluate("innerWidth"), "screenshot": str(path)})
        context.close()


if __name__ == "__main__":
    main()
