"""Continue the same Jupiter alias from its existing about-you session.

No fresh HME, member removal, invitation resend or SMS. Preserve failure-page
screenshots privately and expose only fixed pause categories in the state file.
"""
import asyncio
import inspect
import json
import logging
import os
import time
from pathlib import Path
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)
from app.application.onboard import OnboardService
from app.application.jobs.browser import InvitedBrowserSession
from app.application.operations import operation_store
from app.application.resources import hme
from app.application.workspaces import workspace_service
from app.core.config import load_settings
from app.core.time import utcnow
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account
from app.integrations.openai.browser.signup import SignupBridge
from app.integrations.openai.browser.signup_state import SignupState

EMAIL = '49_nonslip.banks@icloud.com'
_original_handle = SignupState.handle
_original_pump = SignupBridge.pump
PAUSES = {
    '资料提交超过 45 秒仍未完成，请检查网页提示后继续。': 'profile_loading_timeout',
    '表单仍有未填写或格式不正确的内容，请按网页提示修正后继续。': 'form_invalid',
    '姓名输入未完成，请检查网页后继续。': 'name_incomplete',
    '年龄输入未完成，请检查网页后继续。': 'age_incomplete',
    '页面年龄与本次生日不一致，已暂停提交，请检查年龄。': 'age_mismatch',
    '已填写姓名；请手动填写网页的生日控件并提交，再点击插件中的继续。': 'birthday_widget_unknown',
    '找不到可用的提交按钮，请在网页上手动继续。': 'submit_missing',
    '输入框持续重绘或无法聚焦，请检查页面后继续。': 'field_unstable',
    '表单提交后超过 45 秒未前进，请检查网页提示后继续。': 'submission_timeout',
    '点击后超过 45 秒未进入下一步，请检查网络和网页提示后继续。': 'click_timeout',
    '检测到你在手工修改表单，已暂停自动填写；确认后可继续或停止插件。': 'trusted_edit',
}


def observed_handle(self, message, url):
    if message.get('type') == 'pause':
        self.trial_pause = PAUSES.get(message.get('reason'), 'unclassified_pause')
    return _original_handle(self, message, url)


def observed_pump(self):
    _original_pump(self)
    # One explicit, recorded final-button click after the observed submit-event stall.
    # This is a diagnostic intervention, not a change to the shared plugin algorithm.
    if (os.environ.get('JUPITER_FINISH_PROFILE_ONCE') == '1' and self.state.status == 'running'
            and self.state.stage == 'profile' and not getattr(self, 'trial_clicked', False)
            and not self.state.attempts.get('profile')
            and any(e['event'] == 'filled' and e['stage'] == 'profile' for e in self.state.events)):
        valid = self.page.evaluate('''profile => {
          const name=document.querySelector('input[name="name"]'),age=document.querySelector('input[name="age"]');
          const form=name?.form,button=form?.querySelector('button[type="submit"]');
          const [year,month,day]=profile.birthday.split('-').map(Number),today=new Date();
          const expected=today.getFullYear()-year-Number(today.getMonth()+1<month||(today.getMonth()+1===month&&today.getDate()<day));
          return !!(name&&age&&button&&name.value===profile.name&&age.value===String(expected)&&form.checkValidity()
            &&!button.disabled&&button.innerText.trim()==='Finish creating account'
            &&!form.querySelector('[aria-busy="true"],[role="progressbar"],.animate-spin')
            &&document.visibilityState==='visible'&&document.hasFocus());
        }''', self.state.profile)
        if valid:
            self.trial_ready_since = getattr(self, 'trial_ready_since', time.monotonic())
            if time.monotonic() - self.trial_ready_since >= 3:
                self.trial_clicked = True
                self.state.report('trial_profile_submit', '资料页提交事件后未前进，执行一次受控的最终按钮点击')
                self.page.get_by_role('button', name='Finish creating account', exact=True).click(timeout=5000)
        else:
            self.trial_ready_since = time.monotonic()
    if self.state.status == 'paused' and not getattr(self, 'trial_saved', False):
        self.trial_saved = True
        dest = ROOT / 'continuation-debug'
        dest.mkdir(exist_ok=True)
        url = urlsplit(self.page.url)
        proof = {'pause_category': getattr(self.state, 'trial_pause', None), 'error_code': self.state.error_code,
                 'host': url.hostname, 'path': url.path, 'events': self.state.events}
        (dest / 'meta.json').write_text(json.dumps(proof))
        try:
            self.page.screenshot(path=str(dest / 'page.png'), full_page=True, timeout=10000)
        except Exception:
            pass


SignupState.handle = observed_handle
SignupBridge.pump = observed_pump


async def main():
    state = json.loads((ROOT / 'trial-state.json').read_text())
    assert state['workspace_id'] == 11 and state['new_child'] == EMAIL and state['removed']
    assert not state['success']
    state.setdefault('previous_operations', []).append(state['operation_id'])
    engine = create_engine(load_settings())
    factory = create_session_factory(engine)
    original = InvitedBrowserSession.run
    operation = heartbeat = None
    final = {'success': False, 'error_code': 'continuation_interrupted'}

    def progress(**fields):
        state.update(fields, updated_at=utcnow().isoformat())
        temporary = ROOT / 'trial-state.next.json'
        temporary.write_text(json.dumps(state), encoding='utf-8')
        temporary.replace(ROOT / 'trial-state.json')

    async def observed(self, *, on_stage=None, **kwargs):
        if kwargs.get('_invite_onboard'):
            kwargs['start_url'] = 'https://auth.openai.com/about-you'
        async def callback(stage, message):
            if stage != 'heartbeat':
                state['stages'].append(stage)
                state['timeline'].append({'stage': stage, 'at': utcnow().isoformat()})
                progress(browser_stage=stage)
            if on_stage:
                pending = on_stage(stage, message)
                if inspect.isawaitable(pending):
                    await pending
        result = await original(self, on_stage=callback, **kwargs)
        summary = {key: result.get(key) for key in ('ok', 'error_code', 'signup_flow', 'signup_version', 'signup_attempts', 'signup_retries', 'signup_diagnostics')}
        progress(**{('registration_result' if kwargs.get('_invite_onboard') else 'oauth_result'): summary})
        return result

    try:
        async with factory() as db:
            workspace = await workspace_service.load_workspace(db, 11)
            child = await db.get(Account, 305)
            assert workspace.name == 'Jupiter 1' and workspace.owner_account.email == 'hixz2611@gmail.com'
            assert child.email == EMAIL
            assert not await operation_store.browser_busy(db)
            live, item = await workspace_service.lookup_live_member(db, workspace, EMAIL)
            assert live.get('success') and item and item['status'] in ('invited', 'joined')
            operation, blocker = await operation_store.create_workspace_locked(db, op_type='onboard', workspace_id=11,
                account_id=305, email=EMAIL, input_payload={'mode': 'same_email_profile_continuation', 'oauth_signup': True,
                    'role': 'owner', 'seat_intent': 'premium'}, lease_seconds=1200)
            if blocker:
                operation = None
                raise RuntimeError('workspace_busy')
            await db.commit()
            progress(state='continuing_profile', operation_id=operation.public_id, error_code=None)
            async def keep_alive():
                while True:
                    await asyncio.sleep(20)
                    async with factory() as lease_db:
                        await operation_store.heartbeat_active(lease_db, operation.public_id, lease_seconds=1200)
                        await lease_db.commit()
            heartbeat = asyncio.create_task(keep_alive())
            InvitedBrowserSession.run = observed
            final = await OnboardService().invite_and_onboard(db, workspace_id=11, email_line=EMAIL, role='owner',
                seat_intent='premium', oauth_signup=True, phone_line='', browser_executable=load_settings().browser_executable,
                job_id=operation.public_id)
            await db.commit()
            if final.get('joined'):
                live, item = await workspace_service.lookup_live_member(db, workspace, EMAIL)
                assert live.get('success') and item and item['status'] == 'joined' and item['seat_type'] == 'prolite'
                cfg = await hme.load_config(db)
                account = hme.resolve_account(hme.hme_client.list_accounts(cfg), cfg.account_id)
                alias = next(a for a in hme.hme_client.list_aliases(cfg, str(account['id'])) if a.get('email') == EMAIL)
                tag = hme.resolve_workspace_tag(workspace, cfg.team_tag_map)
                hme.hme_client.set_local_label(cfg, str(account['id']), alias['anonymousId'], tag)
                progress(hme_local_label=tag)
    except asyncio.CancelledError:
        final = {'success': False, 'error_code': 'continuation_timeout'}
        raise
    except Exception as exc:
        final = {'success': False, 'error_code': type(exc).__name__}
    finally:
        InvitedBrowserSession.run = original
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        if operation:
            async with factory() as db:
                row = await operation_store.get_by_public_id(db, operation.public_id)
                await operation_store.finish(db, row, final)
                await db.commit()
        progress(state='completed' if final.get('success') else 'stopped', success=bool(final.get('success')),
            error_code=final.get('error_code'), authorized=bool(final.get('authorized')), new_account_id=305, pushed=False)
        await engine.dispose()


if __name__ == '__main__':
    if '--continue-same-email' not in sys.argv:
        raise SystemExit('Explicit same-email continuation required')
    os.umask(0o077)
    marker_name = 'profile-finish-once.lock' if os.environ.get('JUPITER_FINISH_PROFILE_ONCE') == '1' else 'profile-continuation.lock'
    with (ROOT / marker_name).open('x') as marker:
        marker.write(utcnow().isoformat())
    asyncio.run(asyncio.wait_for(main(), timeout=900))
