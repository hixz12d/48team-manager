"""Browser verification of rotation settings; all requests stay in isolated TestClient."""
import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright
from tests.helpers import make_client


async def seed_workspaces(tmp):
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from app.persistence.models.identity import Account, Workspace
    engine = create_async_engine(f"sqlite+aiosqlite:///{(Path(tmp) / 'team48.db').as_posix()}")
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        owner = Account(email="scope-fixture@example.com", local_purpose="mother")
        db.add(owner)
        await db.flush()
        db.add_all([Workspace(owner_account_id=owner.id, official_workspace_id=str(i), name=f"测试团队 {i}", status="active") for i in (1, 2)])
        await db.commit()
    await engine.dispose()


def main():
    with TemporaryDirectory(prefix="team48-night-browser-") as tmp, \
         patch("app.application.jobs.scheduler.in_test_process", return_value=True), \
         make_client(Path(tmp), _env_file=None, official_quota_probe_enabled=False) as client:
        asyncio.run(seed_workspaces(tmp))
        client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors, external, writes = [], [], []
            page.on("pageerror", lambda error: errors.append(str(error)))
            def route(intercept):
                request = intercept.request
                url = urlsplit(request.url)
                if url.netloc != "testserver":
                    external.append(url.netloc); intercept.abort(); return
                if request.method not in {"GET", "HEAD"}:
                    writes.append((request.method, url.path))
                headers = {key: value for key, value in request.headers.items()
                           if key.lower() not in {"host", "cookie", "accept-encoding", "content-length"}}
                response = client.request(request.method, url.path + ("?" + url.query if url.query else ""),
                                          content=request.post_data_buffer, headers=headers)
                headers = {key: value for key, value in response.headers.items()
                           if key.lower() not in {"content-encoding", "content-length", "transfer-encoding", "connection"}}
                intercept.fulfill(status=response.status_code, headers=headers, body=response.content)
            page.route("**/*", route)
            page.goto("http://testserver/accounts")
            toggle = page.locator("#auto-rotation-toggle")
            page.wait_for_function("document.querySelector('#auto-rotation-toggle').textContent === '选择工作空间后启用'")
            assert not writes, writes
            toggle.click()
            page.wait_for_selector('[data-rotation-workspace]')
            page.locator('[data-rotation-workspace][value="1"]').check()
            page.locator('[name="auto_rotate"]').check()
            page.locator('#settings-save').click()
            page.wait_for_function("document.querySelector('#settings-status').textContent.includes('已保存')")
            cfg = client.get('/api/settings').json()['automation']['auto_rotate']
            assert cfg['auto_rotate_scope'] == 'selected' and cfg['auto_rotate_workspace_ids'] == [1]
            page.goto('http://testserver/accounts')
            page.wait_for_function("document.querySelector('#auto-rotation-toggle').getAttribute('aria-pressed') === 'true'")
            assert '仅选中的 1 个工作空间' in page.locator('#auto-rotation-status').inner_text()
            assert client.get("/api/settings").json()["automation"]["auto_rotate"]["auto_rotate_enabled"]
            page.reload()
            page.wait_for_function("document.querySelector('#auto-rotation-toggle').textContent === '关闭自动轮转'")
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 1000})
                assert toggle.is_visible()
                assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
            page.goto("http://testserver/settings")
            page.wait_for_function("document.querySelector('[name=auto_rotate]').checked === true")
            assert page.locator('[data-rotation-workspace][value="1"]').is_checked()
            assert not page.locator('[data-rotation-workspace][value="2"]').is_checked()
            page.locator('#auto-rotation-select-listed').click()
            page.locator("[name=auto_rotate_daily_limit]").fill("6")
            page.locator("#settings-save").click()
            page.wait_for_function("document.querySelector('#settings-status').textContent.includes('已保存')")
            page.reload()
            page.wait_for_function("document.querySelector('[name=auto_rotate_daily_limit]').value === '6'")
            assert page.locator("[name=auto_rotate]").is_checked()
            assert page.locator('[data-rotation-workspace]:checked').count() == 2
            page.locator('[name=auto_rotate_scope]').select_option('all')
            page.locator('#settings-save').click()
            page.wait_for_function("document.querySelector('#settings-status').textContent.includes('已保存')")
            assert client.get('/api/runtime/status').json()['auto_rotation']['scope'] == 'all'
            page.locator("[name=auto_rotate]").uncheck()
            page.locator("#settings-save").click()
            page.wait_for_function("document.querySelector('#settings-status').textContent.includes('已保存')")
            page.goto("http://testserver/accounts")
            page.wait_for_function("document.querySelector('#auto-rotation-toggle').textContent === '开启自动轮转'")
            assert "上限 6 次" in page.locator("#auto-rotation-status").inner_text()
            assert not errors, errors
            assert not external, external
            assert all(path == "/api/settings" for _, path in writes), writes
            browser.close()
            print("PASS: off by default, single/batch/global scope, persistence, master switch, 1440/390px layout; no browser errors or provider writes")


if __name__ == "__main__":
    main()
