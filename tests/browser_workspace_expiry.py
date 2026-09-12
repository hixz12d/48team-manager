"""Real browser checks against a disposable localhost app; no production data or APIs."""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


def preview_app(db_path):
    from fastapi.responses import JSONResponse
    from app.application.jobs import scheduler
    from app.core.config import Settings
    from app.core.time import utcnow, zone
    from app.main import create_app
    from app.persistence.models.identity import Account, Workspace, WorkspaceMembership

    scheduler.in_test_process = lambda: True
    app = create_app(Settings(
        _env_file=None, database_url=f"sqlite+aiosqlite:///{Path(db_path).as_posix()}",
        secret_key="expiry-preview-only", admin_username="preview", admin_password="preview-only",
        session_cookie_secure=False, official_quota_probe_enabled=False,
        auto_reauth_enabled=False, auto_rotate_enabled=False, force_refill=False,
    ))
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with original(app):
            async with app.state.session_factory() as db:
                today = utcnow().astimezone(zone("Asia/Shanghai")).date()
                for index, (name, expires) in enumerate([
                    ("North · 研究团队", None),
                    ("West · 创作团队", today + timedelta(days=4)),
                    ("Sandbox · 测试团队", today - timedelta(days=2)),
                ], 1):
                    owner = Account(email=f"owner{index}@example.com", local_purpose="mother", auth_state="healthy", operational_state="active")
                    db.add(owner)
                    await db.flush()
                    ws = Workspace(name=name, custom_name=name, status="active", owner_account_id=owner.id,
                                   official_workspace_id=f"00000000-0000-0000-0000-{index:012d}",
                                   manual_expires_on=expires, manual_expiry_updated_at=utcnow() if expires else None)
                    db.add(ws)
                    await db.flush()
                    db.add(WorkspaceMembership(workspace_id=ws.id, account_id=owner.id, membership_state="joined", official_role="owner", local_purpose="mother"))
                await db.commit()
            yield
    app.router.lifespan_context = lifespan

    @app.middleware("http")
    async def isolate(request, call_next):
        local_reads = {"/api/accounts", "/api/accounts/portfolio", "/api/workspaces", "/api/overview", "/api/quota/runtime", "/api/runtime/status"}
        expiry_write = request.method == "PATCH" and request.url.path in {f"/api/workspaces/{i}/expiry" for i in (1, 2, 3)}
        allowed = expiry_write or request.url.path in {"/auth/login", "/auth/logout"} or (
            request.method in {"GET", "HEAD"} and (not request.url.path.startswith("/api/") or request.url.path in local_reads))
        if not allowed:
            return JSONResponse({"detail": "隔离预览不执行外部操作"}, status_code=409)
        return await call_next(request)
    return app


def check(base, output):
    from playwright.sync_api import expect, sync_playwright
    errors, writes, rejected = [], [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, timezone_id="America/Los_Angeles")
        assert context.request.post(base + "/auth/login", data={"username": "preview", "password": "preview-only"}).ok
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("response", lambda response: rejected.append(response.url.replace(base, "")) if response.status == 409 else None)
        page.on("request", lambda request: writes.append(request.post_data_json) if request.method == "PATCH" else None)
        page.goto(base + "/accounts")
        first = page.locator('[data-workspace="1"] .workspace-expiry-trigger')
        expect(first).to_contain_text("填写到期日期")
        expect(page.locator('[data-workspace="2"] .workspace-expiry-trigger')).to_contain_text("剩余 4 天")
        expect(page.locator('[data-workspace="3"] .workspace-expiry-trigger')).to_contain_text("已过期 2 天")
        first.click()
        form = page.locator(".workspace-expiry-editor")
        date = form.locator('input[name="expires_on"]')
        expect(date).to_be_focused()
        submit = form.locator('button[type="submit"]')
        expect(submit).to_be_disabled()
        form.get_by_role("button", name="一个月后", exact=True).click()
        target = page.evaluate("Team48Expiry.addMonths(Team48Expiry.today(), 1)")
        expect(date).to_have_value(target)
        # A background refresh must not discard an unsaved draft.
        page.evaluate("Team48Accounts.refresh()")
        expect(date).to_have_value(target)
        submit.click()
        expect(form.locator('[role="status"]')).to_contain_text("已保存")
        expect(first).to_contain_text(target.replace("-", "/"))
        page.keyboard.press("Escape")
        expect(first).to_be_focused()
        page.reload()
        expect(first).to_contain_text(target.replace("-", "/"))
        first.click()
        expect(date).to_have_value(target)
        changed = page.evaluate("Team48Expiry.addMonths(Team48Expiry.today(), 12)")
        date.fill(changed)
        page.route("**/api/workspaces/1/expiry", lambda route: route.fulfill(status=503, json={"detail": "测试网络故障"}), times=1)
        submit.click()
        expect(form.locator('[role="alert"]')).to_contain_text("已保留所填日期")
        expect(date).to_have_value(changed)
        expect(submit).to_be_enabled()
        submit.click()
        expect(form.locator('[role="status"]')).to_contain_text("已保存")
        expect(first).to_contain_text(changed.replace("-", "/"))
        before = len(writes)
        form.get_by_role("button", name="清空日期", exact=True).click()
        expect(submit).to_have_text("保存清空")
        assert len(writes) == before
        form.get_by_role("button", name="撤销修改", exact=True).click()
        expect(date).to_have_value(changed)
        expect(submit).to_be_disabled()
        form.get_by_role("button", name="清空日期", exact=True).click()
        submit.click()
        expect(form.locator('[role="status"]')).to_contain_text("日期已清空")
        expect(first).to_contain_text("填写到期日期")
        page.keyboard.press("Escape")
        page.reload()
        expect(first).to_contain_text("填写到期日期")
        for width in (1440, 768, 390):
            page.set_viewport_size({"width": width, "height": 1000 if width > 540 else 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            page.screenshot(path=str(output / f"teams-{width}.png"), full_page=True)
            page.locator('[data-workspace="2"] .workspace-expiry-trigger').click()
            expect(date).to_be_focused()
            page.screenshot(path=str(output / f"expiry-editor-{width}.png"), full_page=False, animations="disabled")
            bounds = form.evaluate("n => ({right:n.getBoundingClientRect().right, viewport:innerWidth, scroll:n.scrollWidth, client:n.clientWidth})")
            assert bounds["right"] <= width and bounds["scroll"] <= bounds["client"], (width, bounds, str(output))
            page.keyboard.press("Escape")
        assert not errors, errors
        assert not rejected, rejected
        browser.close()
    return {"screenshots": str(output), "widths": [1440, 768, 390], "page_errors": errors,
            "checks": "save/readback, refresh persistence, Beijing dates in US browser, retry, clear/undo, keyboard focus, draft preservation, responsive layout", "rejected": rejected}


def main():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    output = Path(tempfile.mkdtemp(prefix="team48-expiry-review-"))
    print(f"Preview screenshots: {output}", flush=True)
    with tempfile.TemporaryDirectory(prefix="team48-expiry-test-") as tmp:
        with open(Path(tmp) / "server.log", "w", encoding="utf-8") as log:
            process = subprocess.Popen([sys.executable, "-m", "tests.browser_workspace_expiry", "--serve", str(Path(tmp) / "preview.db"), str(port)], stdout=log, stderr=log)
            try:
                for _ in range(200):
                    if process.poll() is not None:
                        raise RuntimeError("Preview server exited; see isolated server log")
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
    if len(sys.argv) > 1 and sys.argv[1] == "--serve":
        import uvicorn
        uvicorn.run(preview_app(sys.argv[2]), host="127.0.0.1", port=int(sys.argv[3]), log_level="warning")
    else:
        main()
