"""Browser contract checks. All mutation requests are intercepted against the local preview."""
import copy
import json
from pathlib import Path
from tempfile import gettempdir
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:8019"
OUT = Path(gettempdir()) / "team48-browser"
OUT.mkdir(exist_ok=True)


def main():
    errors, writes = [], []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1440, "height": 1000})
        assert context.request.post(BASE + "/auth/login", data={"username": "preview", "password": "preview-only"}).ok
        portfolio = context.request.get(BASE + "/api/accounts/portfolio").json()
        workspaces = context.request.get(BASE + "/api/workspaces").json()
        runtime = context.request.get(BASE + "/api/runtime/status").json()
        group = portfolio["groups"][0]
        workspace = workspaces["items"][0]
        email = "member.research@example.com"
        control = {"sync": None, "last": None, "invite_fail": True, "runtime_fail": False}
        op = {"id": "op-preview-sync", "state": "queued", "status": "queued", "stage_label": "排队中", "operation_label": "同步官方成员", "target_label": workspace["display_name"], "trigger_label": "手动", "elapsed_seconds": 0}
        runtime["counts"] = {"running": 1, "queued": 1, "waiting": 0, "waiting_user": 1}
        runtime["active_total"] = 3
        runtime["browser_slot"] = {"state": "busy", "operation_id": None}
        runtime["active_operations"] = [
            {**op, "id": "op-running", "state": "running", "status": "running", "stage_label": "读取成员", "elapsed_seconds": 12},
            {**op, "id": "op-waiting", "status": "waiting_browser", "operation_label": "自动重新授权", "wait_reason": "等待浏览器槽位"},
            {**op, "id": "op-manual", "status": "waiting_user", "operation_label": "手动授权", "wait_reason": "等待人工确认"},
        ]
        runtime["recent_operations"] = [{**op, "id": "op-failed", "state": "failed", "status": "failed", "finished_at": "2026-09-07T08:00:00Z"}]
        for g in portfolio["groups"]:
            g["subscription"] = {"plan_family": "business", "seat_tier": "unknown", "status": "unverified", "observed_at": None}
            for account in g["members"]:
                account["subscription"] = {**g["subscription"], "workspace_id": g["id"]}

        def route_handler(route):
            path = urlparse(route.request.url).path
            method = route.request.method
            if method == "GET" and path == "/api/accounts/portfolio":
                data = copy.deepcopy(portfolio)
                data["groups"][0]["sync_operation"] = control["sync"]
                data["groups"][0]["last_sync_operation"] = control["last"]
                return route.fulfill(json=data)
            if method == "GET" and path == "/api/workspaces":
                return route.fulfill(json=workspaces)
            if method == "GET" and path == "/api/runtime/status":
                return route.fulfill(status=503, json={"error": "test unavailable"}) if control["runtime_fail"] else route.fulfill(json=runtime)
            if method == "GET" and path.startswith("/api/resources/proxies"):
                return route.fulfill(json={"items": []})
            if method != "GET":
                writes.append({"path": path, "body": route.request.post_data_json})
                if path == "/api/workspaces/1/sync":
                    reused = control["sync"] is not None
                    control["sync"] = op
                    return route.fulfill(status=202, json={"ok": True, "status": "queued", "operation_id": op["id"], "workspace_id": 1, "reused": reused})
                if path == "/api/workspaces/2/sync":
                    return route.fulfill(status=400, json={"detail": {"message": "credentials missing", "error_code": "credentials_missing"}})
                if path == "/api/workspaces/sync":
                    control["sync"] = op
                    return route.fulfill(status=202, json={"ok": False, "queued": 2, "reused": 1, "failed": 1, "items": [{"workspace_id": 2, "ok": False, "error_code": "credentials_missing"}]})
                if path == "/api/workspaces/1/kick":
                    for key in ("member_accounts", "official_members"):
                        workspace[key] = [a for a in workspace.get(key, []) if a.get("email") != email]
                    workspace["managed"]["accounts"] = [a for a in workspace["managed"]["accounts"] if a.get("email") != email]
                    workspace["reconciliation"]["items"] = [a for a in workspace["reconciliation"]["items"] if a.get("email") != email]
                    workspace["former_members"] = [{"id": 2, "email": email, "local_account_id": 2, "kind": "departed", "membership_state": "removed", "official_role": "member", "can_reinvite": True}]
                    group["former_members"] = workspace["former_members"]
                    group["members"] = [a for a in group["members"] if a.get("email") != email]
                    return route.fulfill(json={"ok": True, "success": True, "status": "standby", "message": "已离队，账号已保留"})
                if path == "/api/workspaces/1/members/add":
                    if control["invite_fail"]:
                        control["invite_fail"] = False
                        return route.fulfill(status=400, json={"detail": {"message": "官方读取失败，未发送邀请", "error_code": "invite_lookup_unknown"}})
                    workspace["former_members"] = []
                    item = {"id": 2, "email": email, "membership_state": "invited", "official_role": "member", "kind": "invited"}
                    workspace["member_accounts"].append(item)
                    workspace["managed"]["accounts"].append(item)
                    return route.fulfill(json={"ok": True, "success": True, "account_id": 2, "message": "邀请已发送，等待接受"})
                return route.fulfill(status=409, json={"detail": {"message": "Test blocked unplanned write"}})
            return route.continue_()

        context.route("**/api/**", route_handler)
        page = context.new_page()
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto(BASE + "/accounts")
        page.wait_for_selector('[data-workspace-sync="1"]')
        page.evaluate('window.otherTeam = document.querySelector("[data-workspace=\\"2\\"]")')
        page.locator('[data-workspace-sync="1"]').click()
        page.wait_for_function('document.querySelector("[data-workspace-sync=\\"1\\"]").disabled')
        assert page.evaluate('window.otherTeam === document.querySelector("[data-workspace=\\"2\\"]")')
        assert not page.locator('[data-workspace-sync="2"]').is_disabled()
        assert [w["path"] for w in writes] == ["/api/workspaces/1/sync"]
        page.reload()
        page.wait_for_selector('[data-workspace-sync="1"][aria-busy="true"]')
        assert len(writes) == 1
        control["sync"] = None
        control["last"] = {**op, "state": "failed", "status": "failed"}
        page.evaluate("window.Team48Accounts.refresh()")
        page.wait_for_function('!document.querySelector("[data-workspace-sync=\\"1\\"]").disabled')
        assert "最近同步失败" in page.locator('[data-workspace="1"] .group-sync-outcome').inner_text()
        page.screenshot(path=str(OUT / "sync-failed-1440.png"), full_page=True)
        page.locator('[data-workspace-sync="2"]').click()
        page.get_by_text("缺少母号访问凭据，请先手动授权。", exact=True).wait_for()
        assert not page.locator('[data-workspace-sync="2"]').is_disabled()
        page.locator('[data-action-page="workspace-sync-all"]').click()
        page.wait_for_timeout(250)
        assert writes[-1]["path"] == "/api/workspaces/sync"
        assert "团队 #2" in page.locator("#management-sync-status").inner_text()
        page.evaluate("window.Team48Accounts.refresh()")
        assert "缺少母号凭据" in page.locator("#management-sync-status").inner_text()
        control["sync"] = None
        page.evaluate("window.Team48Accounts.refresh()")
        page.locator('[data-focus-key="team:1"]').click()
        row = page.locator(f'.team-member-row[data-member-email="{email}"]')
        row.locator("summary").click()
        page.on("dialog", lambda dialog: dialog.accept())
        row.get_by_role("button", name="移出官方团队").click()
        page.locator(".team-former-members").wait_for()
        page.locator(".team-former-members").get_by_role("button", name="重新邀请").click()
        assert page.locator(".team-reinvite-form select").input_value() == "member"
        page.locator(".team-reinvite-form").get_by_role("button", name="发送邀请").click()
        page.wait_for_function('!document.querySelector(".team-reinvite-form [role=alert]").hidden')
        assert page.locator(".team-reinvite-form select").input_value() == "member"
        page.wait_for_timeout(5000)
        for width in (1440, 390):
            page.set_viewport_size({"width": width, "height": 1000 if width > 500 else 844})
            page.locator(".team-reinvite-form").evaluate("n => n.scrollIntoView({block: 'center'})")
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert page.locator(".team-reinvite-form").evaluate("n => {const r=n.getBoundingClientRect(); return r.right<=innerWidth && r.bottom<=innerHeight;}")
            page.screenshot(path=str(OUT / f"reinvite-{width}.png"))
        page.locator(".team-reinvite-form").get_by_role("button", name="发送邀请").click()
        page.wait_for_function('!document.querySelector(".team-former-members")')
        assert "等待接受邀请" in page.locator(f'.team-member-row[data-member-email="{email}"]').inner_text()
        assert writes[-1]["body"] == {"email": email, "role": "member"}
        assert not any("onboard" in w["path"] or "reauth" in w["path"] or "sub2api" in w["path"] for w in writes)
        page.goto(BASE + "/")
        page.wait_for_selector(".runtime-operation")
        page.wait_for_function('document.querySelector("[data-summary=accounts]").textContent.trim() !== "—"')
        assert page.locator("#runtime-counts").inner_text() == "运行 1 · 排队 1 · 等待 1"
        assert "等待浏览器槽位" in page.locator("#runtime-active").inner_text()
        for width in (1440, 1024, 768, 390):
            page.set_viewport_size({"width": width, "height": 1000 if width > 500 else 844})
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
            assert page.locator("#runtime-status").evaluate("n => n.getBoundingClientRect().right <= innerWidth"), width
            page.screenshot(path=str(OUT / f"runtime-{width}.png"), full_page=True)
        control["runtime_fail"] = True
        page.evaluate("window.Team48Runtime.refresh()")
        page.wait_for_function('!document.querySelector("#runtime-error").hidden')
        assert page.locator(".runtime-operation").count() == 3
        control["runtime_fail"] = False
        page.evaluate("window.Team48Runtime.refresh()")
        page.wait_for_function('document.querySelector("#runtime-error").hidden')
        assert not errors, errors
        print(json.dumps({"screenshots": str(OUT), "page_errors": errors, "intercepted_writes": writes, "checks": "single-team scope, reload recovery, group DOM preserved, explicit batch endpoint, remove/reinvite failure/retry, no onboarding, runtime/error recovery, responsive layouts"}))
        browser.close()


if __name__ == "__main__":
    main()
