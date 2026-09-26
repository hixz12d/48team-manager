"""Third batch: local fixture, compact tables and guarded member workflows."""
import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from playwright.sync_api import sync_playwright, expect

BASE = 'http://127.0.0.1:8019'


def main():
    output=Path('dist/ui-batch3'); output.mkdir(exist_ok=True)
    errors=[]; writes=[]; held=[]
    stamp=datetime.now(timezone.utc)
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True)
        context=browser.new_context(viewport={'width':1440,'height':1000})
        assert context.request.post(BASE+'/auth/login',data={'username':'preview','password':'preview-only'}).ok
        data=context.request.get(BASE+'/api/accounts/portfolio').json()
        groups=data['groups']
        for group in groups:
            group['rotation']={'eligible':True}
            group['former_members']=[]
            for a in group['members']:
                a['health']={'code':'healthy','label':'检测正常','severity':'success','needs_auth':False}
                a['needs_auth']=False
                a['latest_check']={'http_status':200,'current_credential':True,'checked_at':stamp.isoformat()}
                a['quota']=a['last_success_quota']={'five_hour_used_percent':20,'seven_day_used_percent':80,'queried_at':stamp.isoformat(),'seven_day_reset_at':(stamp+timedelta(days=2)).isoformat(),'stale':False}
                a['remote_status']={'state':'healthy','label':'远端正常','severity':'success','remote_id':'9001','checked_at':stamp.isoformat()}
        first=groups[0]; child=first['members'][1]; exhausted=first['members'][2]
        child['health']={'code':'auth_required','label':'授权失效，需要重新登录','severity':'error','needs_auth':True}; child['needs_auth']=True
        child['latest_check']['http_status']=401
        exhausted['quota']['seven_day_used_percent']=100
        first['managed']['accounts']=[dict(a) for a in first['members'] if a['purpose']!='mother']
        first['reconciliation']['items']=[]
        first['member_accounts']=first['managed']['accounts']
        for group in groups:
            group['owner_needs_auth']=False
            group['counts']['health_auth']=sum(bool(a['health']['needs_auth']) for a in group['members'])
            group['counts']['health_retry']=0
        data['summary']['attention']=1
        invited={'email':'pending@example.com','kind':'invited','membership_state':'invited','official_role':'member','managed':False}
        first['managed']['accounts'].append(invited)
        first['former_members']=[{'email':'departed@example.com','id':99,'kind':'departed','membership_state':'removed','official_role':'member','can_reinvite':True}]
        data['accounts']=[a for g in groups for a in g['members']]
        data['unassigned']=[]; data['summary'].update(accounts=len(data['accounts']),needs_auth=1,retry=0)
        data['sub2api_status']={'configured':True,'bindings':0}
        original=copy.deepcopy(data)
        runtime={'counts':{},'runner':{'state':'healthy'},'active_operations':[],'recent_operations':[],'auto_rotation':{'enabled':False,'workspace_ids':[],'daily_limit':2}}
        def route(r):
            url=urlsplit(r.request.url); path=url.path
            if not r.request.url.startswith(BASE): r.abort(); return
            if path=='/api/accounts/portfolio': return r.fulfill(json=data)
            if path=='/api/workspaces': return r.fulfill(json={'items':data['groups']})
            if path=='/api/runtime/status': return r.fulfill(json=runtime)
            if path=='/api/sub2api/push-options': return r.fulfill(json={'groups':[],'proxy_groups':[],'proxies':[]})
            if path.startswith('/api/resources/proxies'): return r.fulfill(json={'items':[]})
            if path.startswith('/api/') and r.request.method not in ('GET','HEAD'):
                writes.append((path,r.request.post_data_json))
                if path.endswith('/rotate') or path.endswith('/replenish'):
                    held.append(r); return
                return r.fulfill(status=409,json={'detail':'Unexpected write blocked by test'})
            return r.continue_()
        context.route('**/*',route)
        page=context.new_page(); page.on('pageerror',lambda err:errors.append(str(err)))
        page.goto(BASE+'/accounts')
        expect(page.locator('.management-table').first).to_be_visible()
        assert not page.locator('.auto-rotation-disclosure').get_attribute('open')
        assert page.locator('.auto-rotation-disclosure').bounding_box()['height']<64
        assert page.locator('[data-workspace="1"] .management-team-controls > button').count()==2
        assert page.locator('[data-workspace="1"] .workspace-switch-counter button').count()==0
        assert not page.locator('[data-workspace="1"]').get_by_role('button',name='删除团队').count()
        assert max(page.locator('.management-table tbody tr').evaluate_all('ns=>ns.map(n=>n.getBoundingClientRect().height)'))<=64
        expect(page.locator('.management-table .management-quota').first).to_contain_text('重置')
        page.locator('[data-focus-key="team:1"]').click()
        rows=page.locator('#sheet-body .team-member-row')
        assert rows.first.get_attribute('data-member-email')==first['owner_email']
        assert rows.nth(1).get_attribute('data-member-email')==child['email']
        assert '已管理账号' not in page.locator('#sheet-body').inner_text()
        page.get_by_role('button',name='邀请成员',exact=True).click()
        invite=page.locator('[data-team-action-forms] form').filter(has=page.locator('textarea[name=email_line]')).filter(has=page.locator('select[name=role]'))
        expect(invite).to_be_visible()
        invite.locator('[name=email_line]').fill('draft@example.com')
        page.get_by_role('button',name='补充团队',exact=True).click()
        expect(invite).to_be_hidden()
        refill=page.locator('[data-team-action-forms] .team-invite-form').filter(has_not=page.locator('textarea'))
        expect(refill).to_be_visible(); expect(refill.locator('[name=role]')).to_have_value('member')
        assert page.locator('[data-team-action-forms] > form:visible').count()==1
        page.get_by_role('button',name='邀请成员',exact=True).click()
        expect(invite.locator('[name=email_line]')).to_have_value('draft@example.com')
        page.get_by_role('button',name='受控轮转',exact=True).click()
        rotation=page.locator('.team-rotation-form'); select=rotation.locator('select[name=email]')
        assert select.locator('option').count()==3
        assert select.locator('option').nth(1).get_attribute('value')==child['email']
        assert select.locator('option').nth(2).get_attribute('value')==exhausted['email']
        expect(select).to_contain_text('5h 20% / 7d 80% · 401')
        assert not writes
        select.select_option(child['email'])
        rotation.get_by_role('button',name='执行受控轮转').click()
        expect(page.locator('#confirm-sheet')).to_contain_text('补位账号继承原角色和席位')
        assert not writes
        page.locator('#confirm-submit').click()
        expect(rotation).to_be_hidden()
        assert writes[0][0]=='/api/workspaces/1/rotate' and writes[0][1]['email']==child['email']
        held.pop().fulfill(json={'success':False,'error':'fixture gate denied'})
        expect(page.locator('#sheet-body .team-primary-actions')).to_be_visible()
        # Keep screenshot fixtures comparable and verify desktop/mobile together.
        page.locator('[data-close-sheet]').click()
        page.reload()
        expect(page.locator('.management-table').first).to_be_visible()
        for theme in ('dark','light'):
            page.locator('[data-theme-select]').select_option(theme)
            for width in (1440,1024,390):
                page.set_viewport_size({'width':width,'height':1000})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.screenshot(path=str(output/f'accounts-{theme}-{width}.png'),animations='disabled')
                page.locator('[data-focus-key="team:1"]').click()
                page.get_by_role('button',name='受控轮转',exact=True).click()
                expect(rotation).to_be_visible()
                assert rotation.evaluate('n=>n.scrollWidth <= n.clientWidth+1')
                page.screenshot(path=str(output/f'team-{theme}-{width}.png'),animations='disabled')
                page.locator('[data-close-sheet]').click()
        # Explicitly absent service removes its whole column, retaining the setup route.
        data['sub2api_status']['configured']=False
        page.evaluate('Team48Accounts.refresh()')
        expect(page.locator('#management-remote-setup')).to_be_visible()
        assert page.locator('.management-table th').filter(has_text='Sub2API').count()==0
        # The recovery action opens the same account draft and never auto-submits.
        child['interrupted_operation_id']='fixture-interrupted'
        page.evaluate('Team48Accounts.refresh()')
        page.locator(f'.management-email[data-focus-key="account:{child["id"]}:1"]').click()
        page.get_by_role('button',name='继续此邮箱').click()
        expect(page.locator('[data-team-action-forms] form:visible textarea[name=email_line]')).to_have_value(child['email'])
        assert len(writes)==1
        page.locator('[data-close-sheet]').click()
        # Forty teams / 240 accounts, quota sort preserves full groups across pages.
        data['groups']=[]; data['accounts']=[]
        for i in range(40):
            g=copy.deepcopy(original['groups'][0]); g['id']=100+i; g['display_name']=f'Team {i:02d}'; g['name']=g['display_name']; g['members']=[]
            for j in range(6):
                a=copy.deepcopy(original['groups'][0]['members'][0]); a.update(id=i*6+j+100,workspace_id=g['id'],email=f'member{i:02d}-{j}@example.com')
                a['last_success_quota']['seven_day_used_percent']=i+j
                g['members'].append(a); data['accounts'].append(a)
            data['groups'].append(g)
        data['summary'].update(teams=40,accounts=240)
        page.goto(BASE+'/accounts?sort=-quota&page_size=20')
        expect(page.locator('.management-group').first).to_contain_text('Team 39')
        assert page.locator('.management-group').count()==3
        assert page.locator('.management-table tbody tr').count()==18
        page.locator('#accounts-pagination').get_by_role('button',name='下一页').click()
        expect(page.locator('.management-group').first).to_contain_text('Team 36')
        assert page.locator('.management-table tbody tr').count()==18
        assert not errors, errors
        print(json.dumps({'ok':True,'page_errors':errors,'rows':240,'widths':[1440,1024,390],'writes':writes},ensure_ascii=False))
        browser.close()


if __name__=='__main__': main()
