"""Local preview only; delete requests are intercepted, no real records deleted."""
import copy
import json
import os
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = os.environ.get('TEAM48_PREVIEW_URL', 'http://127.0.0.1:8019')
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width':1440,'height':1000})
    assert context.request.post(BASE+'/auth/login', data={'username':'preview','password':'preview-only'}).ok
    original = context.request.get(BASE+'/api/accounts/portfolio').json()
    target = original['unassigned'][0]
    extra = {**target, 'id': 9001, 'email': 'standby.extra@example.com'}
    original['unassigned'] = [target, extra]
    original['accounts'] = [item for item in original['accounts'] if item['id'] != extra['id']] + [extra]
    controls = {'deleted': set(), 'fail': True}
    writes, errors, dialogs = [], [], []
    def route_handler(route):
        if route.request.method == 'DELETE':
            assert route.request.url.endswith(f"/api/accounts/{target['id']}")
            writes.append({'kind':'single','body':route.request.post_data_json})
            if controls['fail']:
                return route.fulfill(status=409,json={'detail':{'message':'账号有执行中的任务，请结束任务后再删除。','error_code':'account_busy'}})
            controls['deleted'].add(target['id'])
            return route.fulfill(json={'ok':True,'deleted_account_id':target['id'],'message':'本地账号档案已永久删除，远端账号及别名未改动。'})
        if route.request.url.endswith('/api/accounts/delete-local') and route.request.method == 'POST':
            body = route.request.post_data_json
            writes.append({'kind':'batch','body':body})
            deleted = [{'account_id': account_id, 'email': next(a['email'] for a in original['unassigned'] if a['id']==account_id)} for account_id in body['account_ids']]
            controls['deleted'].update(body['account_ids'])
            return route.fulfill(json={'ok':True,'partial':False,'deleted':deleted,'failed':[],'message':f'已永久删除 {len(deleted)} 个本地档案，远端账号及别名未改动。'})
        if route.request.method != 'GET':
            raise AssertionError('Unexpected write: '+route.request.method)
        if route.request.url.endswith('/api/accounts/portfolio'):
            payload=copy.deepcopy(original)
            payload['unassigned']=[a for a in payload['unassigned'] if a['id'] not in controls['deleted']]
            payload['accounts']=[a for a in payload['accounts'] if a['id'] not in controls['deleted']]
            return route.fulfill(json=payload)
        route.continue_()
    context.route('**/api/**',route_handler)
    page=context.new_page()
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('dialog',lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    page.goto(BASE+'/accounts')
    row=page.locator(f'tr[data-account="{target["id"]}"]')
    extra_row=page.locator(f'tr[data-account="{extra["id"]}"]')
    def open_menu():
        row.get_by_role('button',name='更多操作').click()
    open_menu()
    out=Path(tempfile.gettempdir())/'team48-browser'
    out.mkdir(exist_ok=True)
    page.get_by_role('menuitem',name='永久删除本地档案').click()
    page.get_by_role('dialog',name='永久删除本地档案').wait_for()
    page.screenshot(path=str(out/'delete-confirm-1440.png'))
    page.get_by_role('button',name='取消').first.click()
    assert not writes
    assert page.get_by_role('dialog',name='永久删除本地档案').count()==0
    open_menu()
    page.get_by_role('menuitem',name='永久删除本地档案').click()
    page.get_by_role('button',name='确认删除').click()
    page.get_by_text('账号有执行中的任务，请结束任务后再删除。',exact=True).wait_for()
    assert row.count()==1
    controls['fail']=False
    page.set_viewport_size({'width':390,'height':844})
    open_menu()
    delete_button=page.get_by_role('menuitem',name='永久删除本地档案')
    delete_button.scroll_into_view_if_needed()
    assert delete_button.evaluate('n => {const r=n.getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth;}')
    page.screenshot(path=str(out/'delete-account-390.png'))
    delete_button.click()
    page.get_by_role('button',name='确认删除').click()
    row.wait_for(state='detached')
    page.set_viewport_size({'width':1440,'height':1000})
    extra_row.get_by_label(f'选择 {extra["email"]}').check()
    page.locator('#accounts-search').fill('not-visible@example.com')
    assert page.locator('#account-selection-bar').is_hidden()
    assert page.locator('#account-selection-delete').is_disabled()
    assert len(writes) == 2
    page.locator('#accounts-search').fill('')
    assert not extra_row.get_by_label(f'选择 {extra["email"]}').is_checked()
    extra_row.get_by_label(f'选择 {extra["email"]}').check()
    page.locator('[data-management-view="all"][aria-pressed]').click()
    assert page.locator('#account-selection-bar').is_hidden()
    extra_row.get_by_label(f'选择 {extra["email"]}').check()
    page.get_by_role('button',name='永久删除本地档案').click()
    page.get_by_role('dialog',name='永久删除本地档案').wait_for()
    page.get_by_role('button',name='确认删除 1 个').click()
    extra_row.wait_for(state='detached')
    assert not dialogs
    assert writes[0]['kind']=='single' and writes[0]['body']=={'confirm': True}
    assert writes[1]['kind']=='single' and writes[1]['body']=={'confirm': True}
    assert writes[2]['kind']=='batch' and writes[2]['body']=={'confirm': True, 'account_ids':[extra['id']]}
    assert not errors,errors
    print(json.dumps({'checks':'in-app confirm cancel, server rejection, retry, batch checkbox','page_errors':errors,'screenshots':str(out)}))
    browser.close()
