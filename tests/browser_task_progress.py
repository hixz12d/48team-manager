"""Task progress contract: isolated HTTP, four surfaces, failure/cancel/reload, no providers."""
import copy
import json
import os
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from app.application.presenters import operation_stage_plan
from app.core.time import utcnow
from tests.helpers import make_client


def main():
    now = utcnow()
    op = {"id": "task-onboard-7", "operation_id": "task-onboard-7", "kind": "onboard", "operation": "onboard",
          "operation_label": "邀请入组", "workspace_id": 7, "state": "running", "status": "running",
          "target_label": "North 研究团队", "stage_code": "sms_otp", "current_step": "sms_otp",
          "stage_label": "等待短信验证码", "started_at": (now-timedelta(seconds=130)).isoformat(),
          "stage_plan": operation_stage_plan("onboard"), "observed_stages": ["checking", "inviting", "sms_otp"],
          "can_cancel": True, "can_retry": False, "log": [{"stage": "checking"}, {"stage": "sms_otp"}], "steps": []}
    account = {"id": 1, "email": "member@example.com", "purpose": "child", "managed": True, "workspace_id": 7,
               "health": {"code": "healthy", "label": "正常"}, "remote_status": {"state": "unbound"}}
    workspace = {"id": 7, "name": "North 研究团队", "display_name": "North 研究团队", "members": [account],
                 "managed": {"accounts": [account]}, "member_accounts": [account], "counts": {"joined_people": 1}}
    control = {"offline": False, "active": True, "reads": 0, "cancel": 0}
    errors, external = [], []
    output = Path("dist/ui-batch2"); output.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="team48-progress-") as tmp, \
         patch("app.application.jobs.scheduler.in_test_process", return_value=True), \
         make_client(Path(tmp), _env_file=None, official_quota_probe_enabled=False) as client:
        client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.on("pageerror", lambda err: errors.append(str(err)))
            def route(intercept):
                request = intercept.request; url = urlsplit(request.url)
                if url.netloc != "testserver": external.append(url.netloc); intercept.abort(); return
                if url.path == "/api/runtime/status":
                    control["reads"] += 1
                    if control["offline"]: return intercept.fulfill(status=503, json={})
                    return intercept.fulfill(json={"generated_at": utcnow().isoformat(), "runner": {"state": "healthy"},
                        "counts": {"running": int(control["active"]), "waiting_user": int(op["state"] == "manual_required")},
                        "active_total": int(control["active"]), "active_operations": [op] if control["active"] else [],
                        "recent_operations": [] if control["active"] else [op], "policies": [], "auto_rotation": {"enabled": False}})
                if url.path == "/api/accounts/portfolio":
                    return intercept.fulfill(json={"accounts": [account], "unassigned": [], "groups": [workspace], "summary": {}, "sub2api_status": {"configured": False}})
                if url.path == "/api/workspaces": return intercept.fulfill(json={"items": [workspace]})
                if url.path == "/api/resources/proxies": return intercept.fulfill(json={"items": []})
                if url.path == "/api/sub2api/push-options": return intercept.fulfill(json={"groups": [], "proxy_groups": [], "proxies": []})
                if url.path == "/api/operations/task-onboard-7/cancel":
                    control["cancel"] += 1; op.update(can_cancel=False, cancel_requested=True)
                    return intercept.fulfill(json={"ok": True, "cancel_requested": True})
                if url.path == f"/api/operations/{op['id']}": return intercept.fulfill(json=copy.deepcopy(op))
                if url.path == "/api/workspaces/7/onboard":
                    op.update(id="second-operation", operation_id="second-operation", state="running", status="running",
                              stage_code="sms_otp", stage_label="等待短信验证码", result=None, finished_at=None, can_cancel=True)
                    control.update(active=True, pending_request=intercept)
                    return
                if url.path.startswith("/api/") and request.method != "GET":
                    raise AssertionError(f"unplanned write {request.method} {url.path}")
                headers = {k:v for k,v in request.headers.items() if k.lower() not in {"host","cookie","accept-encoding","content-length"}}
                response = client.request(request.method, url.path+("?"+url.query if url.query else ""), content=request.post_data_buffer, headers=headers)
                safe = {k:v for k,v in response.headers.items() if k.lower() not in {"content-encoding","content-length","transfer-encoding","connection"}}
                intercept.fulfill(status=response.status_code, headers=safe, body=response.content)
            page.route("**/*", route)
            page.goto("http://testserver/accounts")
            card = page.locator('[data-workspace="7"] [data-team-task="7"]')
            expect(card).to_contain_text("第 4/7 步")
            expect(page.locator("#task-center-open")).to_have_text("1 个进行中")
            page.locator('[data-focus-key="team:7"]').click()
            drawer = page.locator('#sheet-body [data-team-task="7"]')
            expect(drawer).to_contain_text("等待短信验证码")
            assert page.locator("#sheet-body").evaluate("n => n.firstElementChild.dataset.teamTask === '7'")
            expect(drawer.locator(".task-steps li")).to_have_count(7)
            expect(drawer.locator(".is-current")).to_have_attribute("aria-current", "step")
            # Time ticks preserve the actual focused button, not just its label.
            cancel = drawer.get_by_role("button", name="取消任务")
            cancel.focus(); page.evaluate("window.focusedTaskButton = document.activeElement")
            page.wait_for_timeout(1150)
            assert page.evaluate("window.focusedTaskButton === document.activeElement && window.focusedTaskButton.isConnected")
            for theme in ("dark", "light"):
                page.evaluate("document.querySelector('[data-close-sheet]').click()")
                page.locator('[data-theme-select]').select_option(theme)
                for width in (1440, 1024, 390):
                    page.set_viewport_size({"width": width, "height": 1000})
                    page.locator('[data-focus-key="team:7"]').click()
                    expect(drawer).to_be_visible()
                    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), width
                    page.screenshot(path=str(output/f"drawer-{theme}-{width}.png"), animations="disabled")
                    page.locator('[data-close-sheet]').click()
                    page.locator('#task-center-open').click()
                    expect(page.locator('#task-center-list')).to_contain_text("第 4/7 步")
                    page.screenshot(path=str(output/f"center-{theme}-{width}.png"), animations="disabled")
                    page.locator('[data-close-tasks]').click()
                page.locator('[data-focus-key="team:7"]').click()
            cancel = drawer.get_by_role("button", name="取消任务")
            cancel.click()
            expect(page.locator('#confirm-sheet')).to_contain_text("已发出的官方邀请不会自动撤回")
            page.locator('#confirm-sheet [data-close-confirm]').last.click()
            assert control['cancel'] == 0
            drawer.get_by_role('button', name='取消任务').click()
            page.locator('#confirm-submit').click()
            expect(drawer).to_contain_text('取消已请求')
            assert control['cancel'] == 1
            page.locator('[data-close-sheet]').click()
            page.locator('#task-center-open').click()
            page.locator('#task-center-list').get_by_role('button', name='查看详情').click()
            expect(page.locator('#sheet-body .task-steps')).to_be_visible()
            expect(page.locator('#task-center')).to_be_hidden()
            expect(page.locator('#sheet-body details').filter(has_text='最近日志')).not_to_have_attribute('open', '')
            page.locator('[data-close-sheet]').click()
            page.reload()
            expect(card).to_contain_text('第 4/7 步')
            # On every page, the same singleton remains the only runtime reader.
            page.goto('http://testserver/settings')
            expect(page.locator('#task-center-open')).to_have_text('1 个进行中')
            before = control['reads']; page.wait_for_timeout(4300)
            assert 1 <= control['reads']-before <= 3, control
            control['offline'] = True
            page.evaluate('window.Team48Runtime.refresh()')
            expect(page.locator('#task-center-open')).to_contain_text('状态更新中断')
            page.locator('#task-center-open').click()
            expect(page.locator('#task-center-list')).to_contain_text('第 4/7 步')
            control['offline'] = False
            control['active'] = False
            op.update(state='partial', status='partial', can_cancel=False, cancel_requested=False, finished_at=utcnow().isoformat(),
                      safe_error_message='手机验证未完成', result={'success':False, 'partial':True, 'error':'手机验证未完成'})
            page.evaluate('window.Team48Runtime.refresh()')
            expect(page.locator('#task-center-list')).to_contain_text('部分完成')
            expect(page.locator('#task-center-list .is-failed .task-step-mark')).to_have_text('!')
            page.locator('[data-close-tasks]').click()
            page.goto('http://testserver/accounts')
            page.locator('[data-focus-key="team:7"]').click()
            page.locator('#sheet-body').get_by_role('button', name='邀请成员', exact=True).click()
            form = page.locator('.team-invite-form').filter(has=page.locator('[name="email_line"]'))
            form.locator('[name="email_line"]').fill('new@example.com')
            form.get_by_role('button', name='发送邀请').click()
            expect(form).to_be_hidden()
            page.evaluate('window.Team48Runtime.refresh()')
            expect(drawer).to_contain_text('等待短信验证码')
            page.locator('[data-close-sheet]').click()
            page.locator('#task-center-open').click()
            expect(page.locator('#task-center-list')).to_contain_text('进行中')
            result = {'success':True, 'status':'active', 'joined':True, 'authorized':True,
                      'pushed':False, 'child':{'email':'new@example.com'}, 'operation_id':'second-operation'}
            op.update(state='success', status='success', result=result, finished_at=utcnow().isoformat(), can_cancel=False)
            control['active'] = False
            control.pop('pending_request').fulfill(json=result)
            page.evaluate('window.Team48Runtime.refresh()')
            expect(page.locator('#task-center-list')).to_contain_text('new@example.com 已入组')
            expect(page.locator('#task-center-list')).to_contain_text('本轮未推送 Sub2API')
            expect(page.locator('#entity-sheet')).to_be_hidden()
            assert not errors, errors
            assert not external, external
            print(json.dumps({'ok':True,'errors':errors,'runtime_reads':control['reads'],'cancel_requests':control['cancel'],
                              'checks':'shared four surfaces, close/reload/navigation, single polling, focus, failure, cancellation confirmation, themes and widths'}))
            if os.environ.get('TEAM48_TASK_DETECT_URL'):
                from urllib.request import urlopen
                messages = []
                page.on('console', lambda message: messages.append(message.text))
                page.evaluate("document.title = '[Human] Task progress review'")
                page.add_script_tag(content="window.__taskDetectorPreflight = true;")
                assert page.evaluate('window.__taskDetectorPreflight')
                page.add_script_tag(content=urlopen(os.environ['TEAM48_TASK_DETECT_URL']).read().decode('utf-8'))
                page.wait_for_timeout(2500)
                (output/'detector.json').write_text(json.dumps(messages, ensure_ascii=False, indent=2), encoding='utf-8')
            browser.close()


if __name__ == '__main__':
    main()
