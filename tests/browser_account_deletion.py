"""Local preview only; delete requests are intercepted, no real records deleted."""
import copy
import json
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

BASE = 'http://127.0.0.1:8019'
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width':1440,'height':1000})
    assert context.request.post(BASE+'/auth/login', data={'username':'preview','password':'preview-only'}).ok
    original = context.request.get(BASE+'/api/accounts/portfolio').json()
    target = original['unassigned'][0]
    controls = {'deleted':False, 'fail':True, 'confirmation':None}
    writes, errors = [], []
    def route_handler(route):
        if route.request.method == 'DELETE':
            assert route.request.url == BASE+f"/api/accounts/{target['id']}"
            writes.append(route.request.post_data_json)
            if controls['fail']:
                return route.fulfill(status=409,json={'detail':{'message':'账号有执行中的任务，请结束任务后再删除。','error_code':'account_busy'}})
            controls['deleted']=True
            return route.fulfill(json={'ok':True,'deleted_account_id':target['id'],'message':'本地账号档案已永久删除，远端账号及别名未改动。'})
        if route.request.method != 'GET':
            raise AssertionError('Unexpected write: '+route.request.method)
        if route.request.url.endswith('/api/accounts/portfolio'):
            payload=copy.deepcopy(original)
            if controls['deleted']:
                payload['unassigned']=[a for a in payload['unassigned'] if a['id'] != target['id']]
                payload['accounts']=[a for a in payload['accounts'] if a['id'] != target['id']]
            return route.fulfill(json=payload)
        route.continue_()
    context.route('**/api/**',route_handler)
    page=context.new_page()
    page.on('pageerror',lambda e:errors.append(str(e)))
    def dialog_handler(dialog):
        if controls['confirmation'] is None: dialog.dismiss()
        else: dialog.accept(controls['confirmation'])
    page.on('dialog',dialog_handler)
    page.goto(BASE+'/accounts')
    row=page.locator(f'tr[data-account="{target["id"]}"]')
    def open_menu():
        row.get_by_role('button',name='更多操作').click()
    open_menu()
    out=Path(tempfile.gettempdir())/'team48-browser'
    out.mkdir(exist_ok=True)
    page.screenshot(path=str(out/'delete-account-1440.png'))
    delete_button=page.get_by_role('menuitem',name='永久删除本地档案')
    delete_button.click()
    assert not writes
    controls['confirmation']='wrong@example.com'
    open_menu(); delete_button.click()
    assert not writes
    controls['confirmation']=target['email']
    open_menu(); delete_button.click()
    page.get_by_text('账号有执行中的任务，请结束任务后再删除。',exact=True).wait_for()
    assert row.count()==1
    controls['fail']=False
    page.set_viewport_size({'width':390,'height':844})
    open_menu()
    delete_button.scroll_into_view_if_needed()
    assert delete_button.evaluate('n => {const r=n.getBoundingClientRect(); return r.left>=0 && r.right<=innerWidth;}')
    page.screenshot(path=str(out/'delete-account-390.png'))
    delete_button.click()
    row.wait_for(state='detached')
    assert len(writes)==2 and all(w=={'confirmation_email':target['email']} for w in writes)
    assert not errors,errors
    print(json.dumps({'checks':'cancel, wrong email, server rejection, retry success, mobile menu','page_errors':errors,'screenshots':str(out)}))
    browser.close()
