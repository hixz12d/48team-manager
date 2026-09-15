"""Browser regression for team proxy editing, with disposable data and a fake remote catalog."""
from contextlib import asynccontextmanager
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from unittest.mock import AsyncMock, patch
from urllib.request import urlopen


def preview_app(db_path):
    from fastapi.responses import JSONResponse
    from app.application.jobs import scheduler
    from app.core.config import Settings
    from app.main import create_app
    from app.persistence.models.identity import Account, Workspace

    scheduler.in_test_process = lambda: True
    app = create_app(Settings(
        _env_file=None, database_url=f"sqlite+aiosqlite:///{Path(db_path).as_posix()}",
        secret_key="proxy-preview-only", admin_username="preview", admin_password="preview-only",
        session_cookie_secure=False, official_quota_probe_enabled=False,
        auto_reauth_enabled=False, auto_rotate_enabled=False, force_refill=False,
    ))
    original = app.router.lifespan_context
    catalog = [{"id": i, "name": f"测试节点 {i}", "protocol": "http", "host": "proxy.example", "port": 8080,
                "username": f"user{i}", "password": f"secret{i}", "status": "disabled" if i == 3 else "active"}
               for i in (1, 2, 3)]

    @asynccontextmanager
    async def lifespan(app):
        with patch("app.integrations.sub2api.client.sub2api_client.list_proxies", AsyncMock(return_value=catalog)), \
             patch("app.integrations.sub2api.client.sub2api_client.load_config", AsyncMock(return_value={"base_url": "https://catalog.invalid"})):
            async with original(app):
                async with app.state.session_factory() as db:
                    for i in (1, 2):
                        owner = Account(email=f"owner{i}@example.com", local_purpose="mother", auth_state="healthy",
                                        proxy=f"http://user{i}:secret{i}@proxy.example:8080", proxy_source="sub2api", sub2api_proxy_id=i)
                        db.add(owner)
                        await db.flush()
                        db.add(Workspace(name=f"测试团队 {i}", owner_account_id=owner.id,
                                         official_workspace_id=f"00000000-0000-0000-0000-{i:012d}"))
                    await db.flush()
                    db.add(Workspace(name="共用母号团队", owner_account_id=1,
                                     official_workspace_id="00000000-0000-0000-0000-000000000003"))
                    await db.commit()
                yield
    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def isolate(request, call_next):
        path = request.url.path
        proxy_write = request.method == "PATCH" and path in {"/api/accounts/1/proxy", "/api/accounts/2/proxy"}
        reads = {"/api/accounts", "/api/accounts/portfolio", "/api/workspaces", "/api/overview",
                 "/api/quota/runtime", "/api/runtime/status", "/api/resources/proxies"}
        if not (proxy_write or path in {"/auth/login", "/auth/logout"} or
                request.method in {"GET", "HEAD"} and (not path.startswith("/api/") or path in reads)):
            return JSONResponse({"detail": "隔离测试不执行外部操作"}, status_code=409)
        return await call_next(request)
    return app


def check(base):
    from playwright.sync_api import expect, sync_playwright
    errors, writes = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        assert context.request.post(base + "/auth/login", data={"username": "preview", "password": "preview-only"}).ok
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" and "OverlayManager" in msg.text else None)
        page.on("request", lambda req: writes.append((req.url, req.post_data_json)) if req.method == "PATCH" else None)
        page.goto(base + "/accounts")
        team = page.locator('[data-workspace="1"]')
        editor = page.locator("#proxy-edit-sheet")
        select = editor.locator('select[name="proxy_remote_id"]')
        submit = editor.get_by_role("button", name="保存", exact=True)
        team.get_by_role("button", name="切换代理", exact=True).click()
        expect(editor).to_be_visible()
        expect(select).to_be_enabled()
        expect(select).to_have_value("1")
        expect(editor.locator('input[name="email"]')).to_have_value("owner1@example.com")
        expect(editor.locator('input[name="current_proxy"]')).to_have_value("http://***:***@proxy.example:8080")
        expect(select.locator('option[value="3"]')).to_have_js_property("disabled", True)
        select.select_option("2")
        # A background refresh preserves the draft and the target mother account.
        page.evaluate("Team48Accounts.refresh()")
        expect(select).to_have_value("2")
        page.route("**/api/accounts/1/proxy", lambda route: route.fulfill(status=503, json={"detail": "测试保存失败"}), times=1)
        submit.click()
        expect(editor.locator("#proxy-edit-status")).to_contain_text("测试保存失败")
        expect(select).to_have_value("2")
        expect(submit).to_be_enabled()
        submit.click()
        expect(editor).to_be_hidden()
        expect(team).to_contain_text("团队代理：Sub2API #2")
        expect(page.locator('[data-workspace="3"]')).to_contain_text("团队代理：Sub2API #2")
        assert all("secret" not in json.dumps(body) for _, body in writes)
        assert writes[-1][1] == {"proxy_selection": {"source": "sub2api", "remote_id": 2}}
        page.reload()
        expect(team).to_contain_text("团队代理：Sub2API #2")
        # Team details must replace the current overlay, then restore it on Escape or save.
        team.get_by_role("button", name="管理团队", exact=True).click()
        details = page.locator("#entity-sheet")
        details.get_by_role("button", name="切换代理", exact=True).click()
        expect(editor).to_be_visible()
        expect(details).to_be_hidden()
        page.keyboard.press("Escape")
        expect(editor).to_be_hidden()
        expect(details).to_be_visible()
        details.get_by_role("button", name="切换代理", exact=True).click()
        expect(select).to_be_enabled()
        editor.locator('input[name="clear"]').check()
        submit.click()
        expect(editor).to_be_hidden()
        expect(details).to_be_visible()
        expect(team).to_contain_text("团队代理：直连")
        expect(page.locator('[data-workspace="2"]')).to_contain_text("团队代理：Sub2API #2")
        expect(page.locator('[data-workspace="3"]')).to_contain_text("团队代理：直连")
        page.keyboard.press("Escape")
        # A failed catalog load cannot silently clear the saved configuration.
        page.route("**/api/resources/proxies?*", lambda route: route.fulfill(status=503, json={"detail": "测试目录不可用"}))
        team.get_by_role("button", name="切换代理", exact=True).click()
        expect(select).to_contain_text("代理目录读取失败")
        before = len(writes)
        submit.click()
        expect(editor.locator("#proxy-edit-status")).to_contain_text("目录尚未加载成功")
        assert len(writes) == before
        page.keyboard.press("Escape")
        page.unroute("**/api/resources/proxies?*")
        # A late response from team 1 must not overwrite team 2's selected proxy.
        held = []
        page.route("**/api/resources/proxies?*", lambda route: held.append(route), times=1)
        team.get_by_role("button", name="切换代理", exact=True).click()
        expect(select).to_contain_text("正在读取代理目录")
        page.keyboard.press("Escape")
        page.locator('[data-workspace="2"]').get_by_role("button", name="切换代理", exact=True).click()
        expect(select).to_be_enabled()
        expect(select).to_have_value("2")
        assert len(held) == 1
        held[0].fulfill(json={"items": [{"id": 99, "name": "旧请求节点", "status": "active"}]})
        expect(select.locator('option[value="2"]')).to_have_count(1)
        expect(select).to_have_value("2")
        page.keyboard.press("Escape")
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 1000})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            team.get_by_role("button", name="切换代理", exact=True).click()
            expect(editor).to_be_visible()
            expect(select).to_be_enabled()
            editor.evaluate("async n => await Promise.all(n.getAnimations({subtree: true}).map(a => a.finished))")
            assert editor.evaluate("n => n.scrollWidth <= innerWidth"), width
            page.keyboard.press("Escape")
        assert not errors, errors
        browser.close()
    return {"checks": "team card/details, masked credentials, shared owner, save/retry/reload, clear, catalog failure, overlay restore, responsive layout", "page_errors": errors}


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="team48-proxy-test-") as tmp:
        log_path = Path(tmp) / "server.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "tests.browser_workspace_proxy", "--serve", str(Path(tmp) / "preview.db"), str(port)], stdout=log, stderr=log)
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
                print(json.dumps(check(base), ensure_ascii=False))
            except Exception:
                print(log_path.read_text(encoding="utf-8")[-6000:])
                raise
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
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        import uvicorn
        uvicorn.run(preview_app(sys.argv[2]), host="127.0.0.1", port=int(sys.argv[3]), log_level="warning")
    else:
        main()
