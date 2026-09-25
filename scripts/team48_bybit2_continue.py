"""Continue only the already-invited Bybit2 HME; no removal or fresh allocation."""
import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)
from sqlalchemy import select
from app.application.onboard import OnboardService
from app.application.jobs.browser import InvitedBrowserSession
from app.application.operations import operation_store
from app.application.resources import hme
from app.application.workspaces import workspace_service
from app.core.config import load_settings
from app.core.time import utcnow
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account

EMAIL = '48.pickets-arrant@icloud.com'


async def main():
    settings = load_settings()
    assert settings.browser_signup_flow == 'extension' and settings.browser_engine == 'chromix'
    state = json.loads((ROOT / 'trial-state.json').read_text())
    assert state['new_child'] == EMAIL and state['removed'] and not state['success']
    state.setdefault('first_operation_id', state['operation_id'])
    state.setdefault('previous_operations', []).append(state['operation_id'])
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    original = InvitedBrowserSession.run
    operation = heartbeat = None
    final = {'success': False, 'error_code': 'continuation_interrupted'}

    def progress(**fields):
        state.update(fields, updated_at=utcnow().isoformat())
        temporary = ROOT / 'trial-state.next.json'
        temporary.write_text(json.dumps(state, ensure_ascii=True), encoding='utf-8')
        temporary.replace(ROOT / 'trial-state.json')

    async def observed(self, *, on_stage=None, **kwargs):
        async def stage_callback(stage, message):
            if stage != 'heartbeat':
                state['stages'].append(stage)
                state['stages'] = state['stages'][-150:]
                progress(browser_stage=stage)
            if on_stage:
                pending = on_stage(stage, message)
                if inspect.isawaitable(pending):
                    await pending
        result = await original(self, on_stage=stage_callback, **kwargs)
        summary = {key: result.get(key) for key in ('ok', 'error_code', 'signup_flow', 'signup_version', 'signup_attempts', 'signup_retries', 'signup_diagnostics')}
        result_key = 'registration_result' if kwargs.get('_invite_onboard') else 'oauth_result'
        progress(browser_result=summary, **{result_key: summary})
        return result

    try:
        async with factory() as db:
            workspace = await workspace_service.load_workspace(db, 17)
            assert workspace.name == 'Bybit2' and workspace.owner_account.email == 'hixz2616@gmail.com'
            child = await db.scalar(select(Account).where(Account.email == EMAIL))
            assert child.id == 300
            assert not await operation_store.browser_busy(db)
            live, item = await workspace_service.lookup_live_member(db, workspace, EMAIL)
            assert live.get('success') and item and item.get('status') in {'invited', 'joined'}
            operation, blocker = await operation_store.create_workspace_locked(db, op_type='onboard', workspace_id=17,
                account_id=child.id, email=EMAIL, input_payload={'mode': 'same_hme_continuation', 'signup_flow': 'extension',
                    'role': 'owner', 'seat_intent': 'premium', 'oauth_signup': True}, lease_seconds=1200)
            if blocker:
                operation = None
                raise RuntimeError('workspace_busy')
            await db.commit()
            progress(state='continuing_from_homepage' if '--homepage' in sys.argv else 'continuing_same_email',
                     operation_id=operation.public_id, error_code=None, final_members=None, final_invites=None)
            async def keep_alive():
                while True:
                    await asyncio.sleep(20)
                    async with factory() as lease_db:
                        await operation_store.heartbeat_active(lease_db, operation.public_id, lease_seconds=1200)
                        await lease_db.commit()
            heartbeat = asyncio.create_task(keep_alive())
            InvitedBrowserSession.run = observed
            final = await OnboardService().invite_and_onboard(db, workspace_id=17, email_line=EMAIL, role='owner',
                seat_intent='premium', oauth_signup=True, phone_line='', browser_executable=settings.browser_executable,
                job_id=operation.public_id)
            await db.commit()
            if final.get('joined'):
                cfg = await hme.load_config(db)
                account = hme.resolve_account(hme.hme_client.list_accounts(cfg), cfg.account_id)
                aliases = hme.hme_client.list_aliases(cfg, str(account['id']))
                alias = next(item for item in aliases if item.get('email') == EMAIL)
                hme.hme_client.set_local_label(cfg, str(account['id']), alias['anonymousId'], hme.resolve_workspace_tag(workspace, cfg.team_tag_map))
    except asyncio.CancelledError:
        final = {'success': False, 'error_code': 'trial_timeout'}
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
            error_code=final.get('error_code'), authorized=bool(final.get('authorized')), new_account_id=300, pushed=False)
        await engine.dispose()


if __name__ == '__main__':
    if '--continue-same-email' not in sys.argv:
        raise SystemExit('Explicit continuation required')
    os.umask(0o077)
    marker_name = 'homepage-continuation-started.lock' if '--homepage' in sys.argv else 'continuation-started.lock'
    with (ROOT / marker_name).open('x') as marker:
        marker.write(utcnow().isoformat())
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=1200))
    except TimeoutError:
        pass
