"""Theme checks in a disposable local preview; no production data or external writes."""
from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def check(base, output):
    from playwright.sync_api import expect, sync_playwright

    errors = []
    paths = ["/", "/accounts", "/operations", "/resources/phones", "/resources/hme", "/resources/proxies", "/settings"]
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, color_scheme="light")
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.goto(base + "/login")
        theme = page.get_by_role("combobox", name="外观模式")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        expect(theme).to_have_value("dark")
        page.screenshot(animations="disabled", path=str(output / "login-dark.png"))

        theme.select_option("light")
        page.reload()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        expect(theme).to_have_value("light")
        theme.select_option("system")
        page.emulate_media(color_scheme="dark")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        page.emulate_media(color_scheme="light")
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        theme.select_option("dark")
        page.emulate_media(color_scheme="dark")
        page.emulate_media(color_scheme="light")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")

        # Preferences sync across tabs, and login/console share the same choice.
        second = context.new_page()
        second.goto(base + "/login")
        second.get_by_role("combobox", name="外观模式").select_option("light")
        expect(theme).to_have_value("light")
        expect(page.locator("html")).to_have_attribute("data-theme", "light")
        second.close()
        assert context.request.post(base + "/auth/login", data={"username": "preview", "password": "preview-only"}).ok
        page.goto(base + "/accounts")
        expect(theme).to_have_value("light")
        page.wait_for_selector(".management-table tbody tr")
        page.screenshot(animations="disabled", path=str(output / "accounts-light.png"))
        theme.select_option("dark")

        # Every console page inherits dark surfaces and fits desktop/tablet/phone.
        for path in paths:
            page.goto(base + path)
            if path == "/accounts":
                page.wait_for_selector(".management-table tbody tr")
            elif path == "/":
                page.wait_for_function("document.querySelector('[data-summary=workspaces]').textContent !== '—'")
            expect(theme).to_have_value("dark")
            expect(page.locator("html")).to_have_attribute("data-theme", "dark")
            assert page.locator(".topbar").evaluate("n => getComputedStyle(n).backgroundColor") == "rgb(26, 34, 48)", path
            assert page.locator(".workspace").evaluate("n => getComputedStyle(n).backgroundColor") == "rgb(17, 22, 30)", path
            for width in (1440, 768, 390):
                page.set_viewport_size({"width": width, "height": 1000 if width > 540 else 844})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (path, width)
                box = theme.bounding_box()
                assert box and box["x"] >= 0 and box["x"] + box["width"] <= width, (path, width, box)
                if path in ("/", "/accounts"):
                    page.screenshot(animations="disabled", path=str(output / f"{'overview' if path == '/' else 'accounts'}-dark-{width}.png"))
            page.set_viewport_size({"width": 1440, "height": 1000})

        page.goto(base + "/accounts")
        page.wait_for_selector(".management-table tbody tr")
        assert page.locator(".management-table-scroll").first.evaluate("n => getComputedStyle(n).backgroundColor") == "rgb(26, 34, 48)"
        page.locator(".workspace-expiry-trigger").first.click()
        expect(page.locator(".workspace-expiry-editor")).to_be_visible()
        assert page.locator(".workspace-expiry-editor").evaluate("n => getComputedStyle(n).backgroundColor") == "rgb(26, 34, 48)"
        page.screenshot(animations="disabled", path=str(output / "expiry-dark.png"))
        page.keyboard.press("Escape")
        page.get_by_role("button", name="登记账号或团队", exact=True).click()
        page.locator("#action-menu").get_by_role("menuitem", name="登记团队", exact=True).click()
        expect(page.locator("#register-sheet")).to_be_visible()
        page.screenshot(animations="disabled", path=str(output / "register-dark.png"))
        page.keyboard.press("Escape")
        page.keyboard.press("Control+k")
        expect(page.locator("#command-palette")).to_be_visible()
        page.screenshot(animations="disabled", path=str(output / "palette-dark.png"))
        page.keyboard.press("Escape")

        page.get_by_role("button", name="删除本地团队：North · 研究团队", exact=True).click()
        expect(page.locator("#confirm-sheet")).to_be_visible()
        assert page.locator(".confirm-panel").evaluate("n => getComputedStyle(n).backgroundColor") == "rgb(26, 34, 48)"
        page.screenshot(animations="disabled", path=str(output / "confirm-dark.png"))
        page.locator("#confirm-sheet .confirm-actions [data-close-confirm]").click()

        # Semantic text stays readable on its matching dark background (WCAG AA).
        contrasts = page.evaluate("""() => {
          const rgb = name => {
            const n = document.createElement('span');
            n.style.color = `var(--${name})`;
            document.body.append(n);
            const value = getComputedStyle(n).color.match(/[\\d.]+/g).slice(0, 3).map(Number);
            n.remove();
            return value;
          };
          const luminance = color => color.map(v => { v /= 255; return v <= .04045 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; })
            .reduce((sum, v, i) => sum + v * [.2126, .7152, .0722][i], 0);
          return [['text', 'surface'], ['muted', 'surface'], ['primary-text', 'primary-bg'],
            ...['danger', 'warning', 'success', 'accent'].map(name => [name + '-text', name + '-bg'])]
            .map(([fg, bg]) => { const a = luminance(rgb(fg)), b = luminance(rgb(bg));
              return [fg, (Math.max(a, b) + .05) / (Math.min(a, b) + .05)]; });
        }""")
        assert all(ratio >= 4.5 for _, ratio in contrasts), contrasts
        page.evaluate("localStorage.setItem('team48-theme', 'invalid')")
        page.reload()
        expect(theme).to_have_value("dark")

        # Restricted storage must not prevent switching or throw on load.
        restricted = browser.new_context()
        restricted.add_init_script("Object.defineProperty(window, 'localStorage', { get() { throw new Error('storage blocked'); } });")
        blocked = restricted.new_page()
        blocked.on("pageerror", lambda error: errors.append(str(error)))
        blocked.goto(base + "/login")
        blocked.get_by_role("combobox", name="外观模式").select_option("light")
        expect(blocked.locator("html")).to_have_attribute("data-theme", "light")
        restricted.close()
        assert not errors, errors
        browser.close()
    return {"checks": "default, persistence, system changes, cross-tab sync, login/console, all pages, responsive layout, overlays, contrast, invalid/blocked storage", "page_errors": errors, "contrast": contrasts, "screenshots": str(output)}


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    output = Path(tempfile.mkdtemp(prefix="team48-theme-review-"))
    print(f"Preview screenshots: {output}", flush=True)
    with tempfile.TemporaryDirectory(prefix="team48-theme-test-") as tmp:
        with open(Path(tmp) / "server.log", "w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "tests.browser_workspace_expiry", "--serve", str(Path(tmp) / "preview.db"), str(port)], stdout=log, stderr=log)
            try:
                for _ in range(200):
                    if process.poll() is not None:
                        raise RuntimeError("Preview server exited")
                    try:
                        with urlopen(base + "/health", timeout=.3) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError("Preview startup timed out")
                print(json.dumps(check(base, output), ensure_ascii=False))
            finally:
                if sys.platform == "win32":
                    subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], capture_output=True)
                else:
                    process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    main()
