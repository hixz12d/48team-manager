"""Isolated browser checks: Sub2API defaults, pagination and bottom batch actions."""
import copy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

from tests.helpers import make_client


def main():
    accounts = [{"id": i, "email": f"child{i:02d}@example.com", "purpose": "child", "managed": True,
                 "can_delete_local": True, "state": "active", "auth": "healthy", "sub2api": "none",
                 "health": {"code": "healthy", "label": "检测正常", "severity": "success", "needs_auth": False},
                 "remote_status": {"state": "unbound", "label": "未绑定 Sub2API"},
                 "workspace_id": 7 if i <= 23 else None} for i in range(1, 46)]
    removed, deletes, errors, external = set(), [], [], []
    options = [{"id": 9, "name": "IPv6 美国", "max_accounts_per_proxy": 2,
                "proxy_ids": [101, 102], "available_proxy_ids": [101, 102]}]
    proxy_groups = AsyncMock(return_value=options)
    with TemporaryDirectory(prefix="team48-defaults-ui-") as tmp, \
         patch("app.application.jobs.scheduler.in_test_process", return_value=True), \
         patch("app.integrations.sub2api.client.sub2api_client.load_config", new=AsyncMock(return_value={"configured": True, "base_url": "http://fixture.invalid"})), \
         patch("app.integrations.sub2api.client.sub2api_client.list_groups", new=AsyncMock(return_value=[{"id": 7, "name": "OpenAI 默认分组", "platform": "openai", "status": "active"}])), \
         patch("app.integrations.sub2api.client.sub2api_client.list_proxy_groups", new=proxy_groups), \
         patch("app.integrations.sub2api.client.sub2api_client.list_proxies", new=AsyncMock(return_value=[{"id": 101, "name": "美国固定出口", "status": "active"}])), \
         make_client(Path(tmp), _env_file=None, official_quota_probe_enabled=False) as client:
        client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda error: errors.append(str(error)))
            def route(intercept):
                request = intercept.request; url = urlsplit(request.url)
                if url.netloc != "testserver":
                    external.append(url.netloc); intercept.abort(); return
                if url.path == "/api/accounts/portfolio":
                    current = [copy.deepcopy(a) for a in accounts if a["id"] not in removed]
                    return intercept.fulfill(json={"accounts": current, "unassigned": [a for a in current if not a["workspace_id"]],
                        "groups": [{"id": 7, "name": "默认团队", "members": [a for a in current if a["workspace_id"]], "counts": {}}],
                        "probe_runtime": {}, "summary": {}, "sub2api_status": {"configured": True, "bindings": 0}})
                if url.path == "/api/accounts/delete-local":
                    body = request.post_data_json; deletes.append(body); removed.update(body["account_ids"])
                    return intercept.fulfill(json={"ok": True, "deleted": [{"account_id": i} for i in body["account_ids"]], "failed": [], "message": "已删除本地档案"})
                headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "cookie", "accept-encoding", "content-length"}}
                response = client.request(request.method, url.path + ("?" + url.query if url.query else ""), content=request.post_data_buffer, headers=headers)
                safe = {k: v for k, v in response.headers.items() if k.lower() not in {"content-encoding", "content-length", "transfer-encoding", "connection"}}
                intercept.fulfill(status=response.status_code, headers=safe, body=response.content)
            page.route("**/*", route)
            page.goto("http://testserver/settings")
            expect(page.locator('[data-service="codex"]')).to_have_count(0)
            expect(page.locator('[name="sub2api_concurrency"]')).to_have_value("5")
            page.get_by_label("OpenAI 默认分组", exact=True).check()
            page.locator('[name="sub2api_concurrency"]').fill("8")
            page.get_by_label("默认代理方式", exact=True).select_option("group")
            page.locator('[name="sub2api_proxy_group_id"]').select_option("9")
            page.get_by_role("button", name="保存设置", exact=True).click()
            expect(page.locator("#settings-status")).to_have_text("已保存 · 刚刚")
            expected = {"concurrency": 8, "group_ids": [7], "proxy_id": None, "proxy_group_id": 9}
            assert client.get("/api/settings").json()["sub2api_push"] == expected
            page.reload()
            expect(page.locator('[name="sub2api_proxy_group_id"]')).to_have_value("9")
            expect(page.get_by_label("OpenAI 默认分组", exact=True)).to_be_checked()
            expect(page.locator('[name="sub2api_concurrency"]')).to_have_value("8")
            page.wait_for_function("!document.getElementById('sub2api-options-refresh').disabled")
            proxy_groups.side_effect = RuntimeError("fixture unavailable")
            page.get_by_role("button", name="刷新分组和代理", exact=True).click()
            expect(page.locator("#sub2api-options-status")).to_contain_text("无法读取")
            expect(page.locator('[name="sub2api_proxy_group_id"]')).to_have_value("9")
            proxy_groups.side_effect = None
            page.get_by_role("button", name="刷新分组和代理", exact=True).click()
            expect(page.locator("#sub2api-options-status")).to_contain_text("分组和代理已更新")
            page.get_by_label("默认代理方式", exact=True).select_option("proxy")
            page.locator('[name="sub2api_proxy_id"]').select_option("101")
            expect(page.locator("#sub2api-proxy-group-field")).to_be_hidden()
            page.get_by_role("button", name="保存设置", exact=True).click()
            expect(page.locator("#settings-status")).to_have_text("已保存 · 刚刚")
            saved = client.get("/api/settings").json()["sub2api_push"]
            assert saved["proxy_id"] == 101 and saved["proxy_group_id"] is None
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 950})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                screenshot(page, f"settings-{width}")
            page.set_viewport_size({"width": 1440, "height": 1000})
            page.goto("http://testserver/accounts")
            rows = page.locator(".management-table tbody tr")
            expect(rows).to_have_count(20)
            expect(page.locator("#accounts-pagination")).to_contain_text("第 1 / 3 页")
            page.get_by_label("选择 child01@example.com", exact=True).check()
            bar = page.locator("#account-selection-bar")
            expect(bar).to_be_visible()
            assert bar.evaluate("n => {const r=n.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight;}"), bar.evaluate("n => {const rows=[]; for(let p=n;p;p=p.parentElement) {const s=getComputedStyle(p), r=p.getBoundingClientRect(); rows.push([p.className,s.position,s.overflow,s.height,r.top,r.bottom]);} return rows;}")
            assert page.evaluate("Boolean(document.getElementById('accounts-pagination').compareDocumentPosition(document.getElementById('account-selection-bar')) & Node.DOCUMENT_POSITION_FOLLOWING)")
            page.get_by_role("button", name="下一页", exact=True).click()
            expect(rows).to_have_count(20)
            expect(page.locator("#account-selection-count")).to_contain_text("已选 1 个")
            expect(page.locator('.management-group [data-account="21"]')).to_have_count(1)
            page.get_by_label("选择 child21@example.com", exact=True).check()
            expect(page.locator("#account-selection-count")).to_contain_text("已选 2 个")
            page.get_by_role("button", name="上一页", exact=True).click()
            expect(page.get_by_label("选择 child01@example.com", exact=True)).to_be_checked()
            page.locator("#account-selection-delete").click()
            expect(page.get_by_role("dialog", name="永久删除本地档案")).to_be_visible()
            page.get_by_role("button", name="确认删除 2 个", exact=True).click()
            expect(page.locator("#accounts-pagination")).to_contain_text("共 43 条")
            assert deletes == [{"confirm": True, "account_ids": [1, 21]}], deletes
            expect(bar).to_be_hidden()
            page.get_by_role("button", name="下一页", exact=True).click()
            page.get_by_role("button", name="下一页", exact=True).click()
            expect(rows).to_have_count(3)
            page.get_by_label("全选本地账号", exact=True).check()
            page.locator("#account-selection-delete").click()
            page.get_by_role("button", name="确认删除 3 个", exact=True).click()
            expect(page.locator("#accounts-pagination")).to_contain_text("第 2 / 2 页")
            expect(rows).to_have_count(20)
            page.locator("#accounts-search").fill("child02@example.com")
            expect(rows).to_have_count(1)
            expect(page.locator("#accounts-pagination")).to_contain_text("第 1 / 1 页")
            page.locator("#accounts-search").fill("")
            page.get_by_label("每页条数", exact=True).select_option("10")
            expect(rows).to_have_count(10)
            page.get_by_role("button", name="下一页", exact=True).click()
            page.reload()
            expect(page.locator("#accounts-pagination")).to_contain_text("第 2 / 4 页")
            expect(rows).to_have_count(10)
            page.get_by_label("选择 child12@example.com", exact=True).check()
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 950})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                assert bar.evaluate("n => {const r=n.getBoundingClientRect(); return r.top >= 0 && r.bottom <= innerHeight;}")
                screenshot(page, f"accounts-{width}")
            assert not errors, errors
            assert not external, external
            print(json.dumps({"ok": True, "checks": ["settings save/reload", "proxy group and fixed proxy", "catalog failure preserves draft", "Codex Proxy removed", "pagination with team context", "cross-page selection/delete", "last-page clamp", "search resets page", "page size and URL persist", "bottom actions", "responsive layout"], "page_errors": errors}))
            browser.close()


def screenshot(page, name):
    if os.environ.get("TEAM48_DEFAULTS_SCREENSHOTS"):
        folder = Path(os.environ["TEAM48_DEFAULTS_SCREENSHOTS"]); folder.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(folder / (name + ".png")), full_page=True)


if __name__ == "__main__":
    main()
