"""Real Chromium + isolated FastAPI/SQLite. All browser HTTP stays in TestClient."""
import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from app.persistence.models.identity import Account, ExternalBinding
from tests.helpers import make_client
from tests.test_sub2api_remote_state import fixture, INSTANCE


def main():
    observed = {"snapshot": fixture(), "fail": False, "reads": 0, "retries": 0}
    observed["readbacks"] = 0
    observed["recoveries"] = 0
    observed.update(handoff_waiting=True, handoff_calls=[], handoff_ids=set())
    async def source(*_args):
        observed["reads"] += 1
        return {"ok": False, "error_code": "bridge_unavailable"} if observed["fail"] else {"ok": True, "snapshot": copy.deepcopy(observed["snapshot"])}
    async def retry(_client, _db, remote_id, op_id, instance):
        assert remote_id == 42 and op_id == "fixture-operation-2" and instance == INSTANCE
        observed["retries"] += 1
        return {"ok": True, "queued": True}
    async def readback(_client, _db, remote_id, instance, version, stamp):
        observed["readbacks"] += 1
        return {"ok": True, "remote_account_id": remote_id, "instance_id": instance, "credential_version": version,
                "account_updated_at": stamp, "refresh_configured": True, "access_token": "fixture-readback-AT", "client_id": "fixture-client"}
    async def recover_bridge(_db, remote_id, **kwargs):
        assert remote_id == 42 and kwargs["recovery_mode"] == "auth_only"
        observed["recoveries"] += 1
        observed["snapshot"].update(credential_version=9, account_updated_at="2026-09-11T12:03:00+00:00")
        observed["snapshot"]["latest_operation"].update(operation_id=kwargs["operation_id"], credential_version=9, updated_at="2026-09-11T12:03:00+00:00",
            auth_recovery="cleared", validation_scope="codex_identity_usage_catalog", validated_at="2026-09-11T12:03:00+00:00", state="completed", token_cache_invalidation="succeeded", scheduler_refresh="succeeded")
        return {"ok": False, "credential_write": "succeeded", "auth_recovery": "cleared", "validation_scope": "codex_identity_usage_catalog", "remaining_blockers": ["schedulable_off"]}
    async def handoff_bridge(_db, remote_id, request, action):
        assert remote_id == 42
        observed["handoff_calls"].append(action); observed["handoff_ids"].add(request["operation_id"])
        data = {"ok": True, "state": "acknowledged" if action == "ack" else "draining" if observed["handoff_waiting"] else "ready", "epoch": 2, "credential_version": 10}
        if action == "read":
            data["credentials"] = {"access_token": "fixture-handoff-AT", "refresh_token": "fixture-handoff-RT", "client_id": "fixture-client"}
        return data
    with TemporaryDirectory(prefix="team-sync-batch5-browser-") as tmp, \
         patch("app.application.jobs.scheduler.in_test_process", return_value=True), \
         patch("app.application.sub2api_remote_state.request_state", new=source), \
         patch("app.application.sub2api_remote_state.retry_followups", new=retry), \
         patch("app.application.sub2api_refresh_authority.request_access_token", new=readback), \
         patch("app.application.sub2api_refresh_authority._encrypt", return_value="fixture-sealed-AT"), \
         patch("app.application.sub2api_auth_recovery.sub2api_client.sync_oauth_credentials", new=recover_bridge), \
         patch("app.application.sub2api_auth_recovery._build_credentials", return_value={"access_token": "fixture-recovery-AT", "refresh_token": "fixture-recovery-RT", "client_id": "fixture-client"}), \
         patch("app.application.sub2api_refresh_handoff.handoff_call", new=handoff_bridge), \
         patch("app.application.sub2api_refresh_handoff._encrypt", side_effect=lambda value: "sealed:" + value), \
         make_client(Path(tmp), _env_file=None, official_quota_probe_enabled=False) as client:
        # The real endpoints must reject unauthenticated requests.
        assert client.get("/api/accounts/1/sub2api/remote-state").status_code == 401
        assert client.post("/api/accounts/1/sub2api/remote-state/retry").status_code == 401
        assert client.get("/api/accounts/1/sub2api/refresh-authority").status_code == 401
        assert client.post("/api/accounts/1/sub2api/refresh-authority/preview").status_code == 401
        assert client.post("/api/accounts/1/sub2api/refresh-authority", json={}).status_code == 401
        assert client.post("/api/accounts/1/sub2api/auth-recovery/preview").status_code == 401
        assert client.post("/api/accounts/1/sub2api/auth-recovery", json={}).status_code == 401
        assert client.post("/api/accounts/1/sub2api/refresh-return/preview").status_code == 401
        assert client.post("/api/accounts/1/sub2api/refresh-return", json={}).status_code == 401
        assert observed["reads"] == 0 and observed["retries"] == 0
        assert client.post("/auth/login", json={"username": "hixz12", "password": "test-password"}).status_code == 200
        async def seed():
            async with client.app.state.session_factory() as db:
                account = Account(email="billing@example.com", official_account_id="acct-billing", auth_state="healthy", operational_state="active", local_purpose="child")
                db.add(account); await db.flush()
                db.add(ExternalBinding(provider="sub2api", local_account_id=account.id, remote_account_id="42", binding_state="verified", verified_email=account.email, verified_official_account_id="acct-billing"))
                await db.commit()
                return account.id
        account_id = client.portal.call(seed)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors, external = [], []
            page.on("pageerror", lambda err: errors.append(str(err)))
            def route(request_route):
                request = request_route.request
                url = urlsplit(request.url)
                if url.netloc != "testserver":
                    external.append(url.netloc); request_route.abort(); return
                headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "cookie", "accept-encoding", "content-length"}}
                response = client.request(request.method, url.path + ("?" + url.query if url.query else ""), content=request.post_data_buffer, headers=headers)
                safe_headers = {k: v for k, v in response.headers.items() if k.lower() not in {"content-encoding", "content-length", "transfer-encoding", "connection"}}
                request_route.fulfill(status=response.status_code, headers=safe_headers, body=response.content)
                assert b"fixture-readback-AT" not in response.content, "secret reached browser"
                assert b"fixture-recovery-AT" not in response.content and b"fixture-recovery-RT" not in response.content
                assert b"fixture-handoff-AT" not in response.content and b"fixture-handoff-RT" not in response.content
            page.route("**/*", route)
            page.goto(f"http://testserver/accounts?view=all&account={account_id}")
            panel = page.locator("[data-sub2api-state]")
            panel.wait_for()
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('尚未核对')")
            check = panel.get_by_role("button", name="重新核对状态")
            retry_button = panel.get_by_role("button", name="重试未完成步骤")
            assert retry_button.is_disabled()
            check.click()
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('后续处理中')")
            assert retry_button.is_enabled()
            observed["snapshot"] = fixture(state="completed")
            observed["snapshot"]["latest_operation"]["updated_at"] = "2026-09-11T12:01:00+00:00"
            check.click()
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('同步步骤已完成')")
            assert "仍有运行阻断" in panel.inner_text()
            assert "人工调度开关关闭" in panel.inner_text()
            assert retry_button.is_disabled()
            observed["fail"] = True
            check.click()
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('旧快照')")
            assert retry_button.is_disabled()
            observed["fail"] = False
            observed["snapshot"]["instance_id"] = "b69d02db-8fb2-4f79-bef2-482476b1c100"
            check.click()
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('实例已变化')")
            assert retry_button.is_disabled()
            for width in (1440, 390):
                page.set_viewport_size({"width": width, "height": 1000})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                assert panel.evaluate("node => node.scrollWidth <= node.clientWidth + 1"), width
            observed["snapshot"] = fixture()
            observed["snapshot"].update(credential_version=8, account_updated_at="2026-09-11T12:02:00+00:00")
            observed["snapshot"]["latest_operation"].update(operation_id="fixture-operation-2", credential_version=8, updated_at="2026-09-11T12:02:00+00:00")
            check.click()
            page.wait_for_function("!document.querySelector('[data-sub2api-state] button:last-child').disabled")
            retry_button.click()
            page.wait_for_function("!document.querySelector('[data-sub2api-state] button').disabled")
            assert observed["retries"] == 1
            observed["snapshot"]["access_token_readback"] = True
            delegate = panel.get_by_role("button", name="委托远端刷新", exact=True)
            page.once("dialog", lambda dialog: dialog.dismiss())
            delegate.click()
            page.wait_for_function("!document.querySelector('[data-sub2api-state] button').disabled")
            assert observed["readbacks"] == 0, "preview/cancel fetched a secret"
            page.once("dialog", lambda dialog: dialog.accept())
            delegate.click()
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('当前刷新方：Sub2API')")
            assert observed["readbacks"] == 1
            assert panel.get_by_role("button", name="重新采用远端访问令牌").is_visible()
            async def inspect_adopted():
                async with client.app.state.session_factory() as db:
                    account = await db.get(Account, account_id)
                    assert account.access_token_encrypted == "fixture-sealed-AT"
                    assert account.refresh_token_encrypted is None
                    assert account.auth_state == "healthy"
            client.portal.call(inspect_adopted)
            async def local_reauthorized():
                async with client.app.state.session_factory() as db:
                    account = await db.get(Account, account_id)
                    account.access_token_encrypted = "fixture-sealed-new-AT"
                    account.refresh_token_encrypted = "fixture-sealed-new-RT"
                    account.auth_state = "oauth_required"
                    await db.commit()
            client.portal.call(local_reauthorized)
            recover_button = panel.get_by_role("button", name="验证新授权并恢复认证")
            page.once("dialog", lambda dialog: dialog.dismiss())
            recover_button.click()
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('已取消，未提交新授权')")
            assert observed["recoveries"] == 0
            page.once("dialog", lambda dialog: dialog.accept())
            recover_button.click()
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('匹配的认证错误已清除')")
            assert observed["recoveries"] == 1
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('此版本身份与 Codex 服务访问已验证')")
            handback = panel.get_by_role("button", name="安全交回 Team", exact=True)
            page.once("dialog", lambda dialog: dialog.dismiss())
            handback.click()
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('已取消交回')")
            assert observed["handoff_calls"] == []
            page.once("dialog", lambda dialog: dialog.accept())
            handback.click()
            panel.get_by_role("button", name="继续交回 Team", exact=True).wait_for()
            assert observed["handoff_calls"] == ["prepare"]
            observed["handoff_waiting"] = False
            page.once("dialog", lambda dialog: dialog.accept())
            panel.get_by_role("button", name="继续交回 Team", exact=True).click()
            page.wait_for_function("document.querySelector('[data-sub2api-state]').textContent.includes('当前刷新方：Team（已安全交回）')")
            assert observed["handoff_calls"] == ["prepare", "prepare", "read", "ack"]
            assert len(observed["handoff_ids"]) == 1
            async def inspect_returned():
                async with client.app.state.session_factory() as db:
                    account = await db.get(Account, account_id)
                    assert account.refresh_token_encrypted == "sealed:fixture-handoff-RT"
                    assert account.auth_state == "oauth_required"
            client.portal.call(inspect_returned)
            assert '人工调度开关关闭' in panel.inner_text()
            assert panel.evaluate("node => node.scrollWidth <= node.clientWidth + 1")
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)
            reads_at_close = observed["reads"]
            page.wait_for_timeout(5500)
            assert observed["reads"] == reads_at_close, "closed drawer kept polling the remote"
            assert not errors, errors
            print(json.dumps({"checks": "authenticated API, authority/recovery/reverse-handoff preview and cancellation, drain/resume same operation, paused/auth state retained, no browser secrets, drawer cleanup", "widths": [1440, 390], "page_errors": errors, "remote_retries": observed["retries"], "access_readbacks": observed["readbacks"], "recoveries": observed["recoveries"], "handoff_calls": observed["handoff_calls"], "external_requests_blocked": external}, ensure_ascii=False))
            browser.close()


if __name__ == "__main__":
    main()
