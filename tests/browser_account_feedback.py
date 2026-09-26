"""Isolated real-browser regression for authorization feedback and remote presence."""
import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qs, urlencode, urlsplit
from unittest.mock import AsyncMock, patch

import httpx
from playwright.sync_api import expect, sync_playwright

from app.core.time import utcnow
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot
from tests.helpers import make_client


def main():
    remotes = [{"id": 42, "status": "active", "schedulable": True, "credentials": {"email": "existing@example.com"}}]
    runtime = {"failure": False, "reads": 0, "email": "new@example.com"}
    pushes = []
    async def inventory(*args):
        runtime["reads"] += 1
        if runtime["failure"]:
            raise httpx.ConnectError("fixture offline")
        return copy.deepcopy(remotes)
    with TemporaryDirectory(prefix="team48-feedback-") as tmp, \
         patch("app.application.jobs.scheduler.in_test_process", return_value=True), \
         patch("app.application.sub2api_status.REFRESH_SECONDS", 0), \
         patch("app.integrations.sub2api.client.sub2api_client.load_config", new=AsyncMock(return_value={"configured": True, "base_url": "http://fixture.invalid"})), \
         patch("app.integrations.sub2api.client.sub2api_client.list_status_accounts", new=inventory), \
         patch("app.integrations.sub2api.client.sub2api_client.list_proxies", new=AsyncMock(return_value=[])), \
         patch("app.application.reauth.chatgpt_client.exchange_oauth_code", new=AsyncMock(return_value={"success": True, "access_token": "fixture-at", "refresh_token": "fixture-rt"})), \
         patch("app.core.jwt.jwt_parser.extract_email", side_effect=lambda _: runtime["email"]), \
         patch("app.application.reauth.push_refreshed_tokens_to_bound_sub2api", new=AsyncMock(return_value={"ok": True})), \
         make_client(Path(tmp), _env_file=None, official_quota_probe_enabled=False) as client:
        assert client.post("/api/sub2api/status/refresh").status_code == 401
        assert runtime["reads"] == 0
        assert client.post("/auth/login", json={"username": "hixz12", "password": "test-password"}).status_code == 200
        async def seed():
            async with client.app.state.session_factory() as db:
                owner = Account(email="owner@example.com", local_purpose="mother", auth_state="healthy")
                child = Account(email="existing@example.com", local_purpose="child", auth_state="oauth_required")
                db.add_all([owner, child]); await db.flush()
                ws = Workspace(name="Feedback Team", official_workspace_id="11111111-1111-4111-8111-111111111111",
                               owner_account_id=owner.id, status="active", last_official_sync_at=utcnow())
                db.add(ws); await db.flush()
                db.add(WorkspaceMembership(workspace_id=ws.id, account_id=child.id, local_purpose="child", membership_state="joined"))
                for email in (owner.email, child.email, "new@example.com"):
                    db.add(WorkspaceOfficialMemberSnapshot(workspace_id=ws.id, normalized_email=email, remote_state="joined", fetched_at=utcnow()))
                db.add(ExternalBinding(provider="sub2api", workspace_id=ws.id, local_account_id=child.id, remote_account_id="42",
                                       binding_state="verified", verified_email=child.email))
                await db.commit()
                return child.id
        child_id = client.portal.call(seed)
        errors, external = [], []
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            def route(intercept):
                request = intercept.request
                if request.method == "POST" and any(request.url.split("?")[0].endswith(f"/sub2api/{action}") for action in ("preview", "push")):
                    pushes.append(request.url)
                    intercept.fulfill(json={"ok": True, "action": "create", "message": "Sub2API 推送完成"})
                    return
                url = urlsplit(request.url)
                if url.netloc != "testserver":
                    external.append(url.netloc); intercept.abort(); return
                headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "cookie", "accept-encoding", "content-length"}}
                response = client.request(request.method, url.path + ("?" + url.query if url.query else ""), content=request.post_data_buffer, headers=headers)
                safe = {k: v for k, v in response.headers.items() if k.lower() not in {"content-encoding", "content-length", "transfer-encoding", "connection"}}
                intercept.fulfill(status=response.status_code, headers=safe, body=response.content)
            page.route("**/*", route)
            page.goto("http://testserver/accounts")
            existing = page.locator(f'.management-table tr[data-account="{child_id}"]')
            existing.locator('[data-remote-state="healthy"]').wait_for()
            expect(existing.get_by_role("button", name="更新 Sub2API", exact=True)).to_have_count(0)
            # One click from the list opens the actual link + authorization flow.
            page.get_by_role("button", name="接入并授权", exact=True).click()
            page.locator(".team-auth-form").wait_for()
            page.wait_for_function("document.querySelector('.team-auth-form textarea').value.includes('state=')")
            link = page.locator(".team-auth-form textarea").first.input_value()
            state = parse_qs(urlsplit(link).query)["state"][0]
            callback = "http://localhost:1455/auth/callback?" + urlencode({"state": state, "code": "fixture-code"})
            # Follow-ups default on; keep the manual push path under test here, but count the switch.
            expect(page.get_by_label("今日切换 +1（同一成员只计一次）")).to_be_checked()
            page.get_by_label("推送到 Sub2API（使用设置里的默认分组和代理）").uncheck()
            page.locator(".team-auth-form textarea").nth(1).fill(callback)
            page.get_by_role("button", name="完成授权", exact=True).click()
            page.locator(".toast").filter(has_text="切换次数：今日切换 +1，现为 1 次").wait_for()
            new_row = page.locator(".management-table tr").filter(has_text="new@example.com")
            # Rows show the short badge; the full health label lives in its tooltip and the detail drawer.
            new_row.locator('.management-badge[title^="已授权 · 等待额度检测"]').wait_for()
            page.keyboard.press("Escape")
            expect(new_row.get_by_role("button", name="详情", exact=True)).to_have_count(1)
            push = new_row.get_by_role("button", name="推送到 Sub2API", exact=True)
            # Batch 3: each row keeps one primary action (授权 / 详情); push is secondary.
            expect(push).to_have_class("button")
            push.click()
            expect(push).to_be_enabled()
            assert len(pushes) == 2, pushes
            assert "/sub2api/preview?workspace_id=" in pushes[0], pushes
            assert "/sub2api/push?workspace_id=" in pushes[1], pushes
            assert urlsplit(pushes[0]).query == urlsplit(pushes[1]).query
            # Standalone authorization also refreshes the main list immediately.
            runtime["email"] = "existing@example.com"
            existing.get_by_role("button", name="授权", exact=True).click()
            page.wait_for_function("document.querySelector('#reauth-authorize-url').value.includes('state=')")
            state = parse_qs(urlsplit(page.locator("#reauth-authorize-url").input_value()).query)["state"][0]
            # Pasting a complete callback submits it without clicking the button.
            page.locator('#reauth-form [name="callback_url"]').focus()
            page.evaluate("text => { const field = document.querySelector('#reauth-form [name=callback_url]'); field.value = text;"
                          " field.dispatchEvent(new ClipboardEvent('paste', {bubbles: true})); }",
                          "http://localhost:1455/auth/callback?" + urlencode({"state": state, "code": "fixture-code-2"}))
            existing.locator('.management-badge[title^="已授权 · 等待额度检测"]').wait_for()
            # A complete empty remote inventory must show deletion, not quota alone.
            expect(existing.get_by_role("button", name="更新 Sub2API", exact=True)).to_be_visible()
            page.wait_for_load_state("networkidle")
            remotes.clear()
            existing.locator('[data-remote-state="missing"]').wait_for(timeout=45000)
            # Batch 3 moved the remote filter and the check button into "更多筛选".
            page.locator('.management-more-filters > summary').click()
            page.locator("#management-remote").select_option("missing")
            assert page.locator(".management-table tbody tr").count() == 1
            assert "existing@example.com" in page.locator(".management-table tbody tr").inner_text()
            page.locator("#management-remote").select_option("all")
            runtime["failure"] = True
            page.get_by_role("button", name="核对 Sub2API 状态", exact=True).click()
            # Row shows a short failure badge plus the last known state; the full reason is in the status line and tooltip.
            existing.get_by_text("核对失败", exact=True).wait_for()
            assert "上次：远端已删除" in existing.inner_text()
            assert "暂时无法核对 Sub2API" in existing.locator(".management-money").get_attribute("title")
            runtime["failure"] = False
            remotes.append({"id": 42, "status": "inactive", "schedulable": False, "credentials": {"email": "existing@example.com"}})
            page.get_by_role("button", name="核对 Sub2API 状态", exact=True).click()
            existing.locator('[data-remote-state="paused"]').wait_for()
            for width in (1440, 1024, 390):
                page.set_viewport_size({"width": width, "height": 900})
                # The row actions must stay inside their cell without overlapping the check time.
                assert new_row.locator(".management-actions").evaluate("node => { const cell = node.parentElement.getBoundingClientRect(); return [...node.children].every(button => { const box = button.getBoundingClientRect(); return box.left >= cell.left && box.right <= cell.right; }); }"), width
                if os.environ.get("TEAM48_FEEDBACK_SCREENSHOTS"):
                    output = Path(os.environ["TEAM48_FEEDBACK_SCREENSHOTS"])
                    output.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(output / f"accounts-{width}.png"), full_page=True)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            assert not errors, errors
            assert not external, external
            print(json.dumps({"ok": True, "checks": ["direct link", "team OAuth refresh", "inline push with workspace context", "standalone OAuth refresh", "paste auto-submit", "switch follow-up", "inline update", "remote deletion", "remote filter", "network failure", "pause readback", "responsive layout"], "page_errors": errors}))
            browser.close()


if __name__ == "__main__":
    main()
