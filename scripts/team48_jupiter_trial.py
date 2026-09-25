"""One approved Jupiter 1 child replacement in an isolated test directory.

Keep the mother, replace the exact exhausted child, acquire one fresh HME and
stop at phone verification. No rotate, SMS pool, Sub2API push or service restart.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import inspect
import json
import logging
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
logging.disable(logging.CRITICAL)
from app.application.onboard import OnboardService
from sqlalchemy import select
from app.application import invitation_flow
from app.application.jobs.browser import InvitedBrowserSession
from app.application.member_lifecycle import record_confirmed_departure, has_other_active_context
from app.application.operations import operation_store
from app.application.reauth import load_cf_config
from app.application.resources import hme
from app.application.tokens import decrypt_secret
from app.application.workspaces import workspace_service
from app.core.config import load_settings
from app.core.time import utcnow
from app.domain.onboard import KICK_COOLDOWN_SECONDS
from app.integrations.openai.chatgpt import chatgpt_client
from app.integrations.openai.browser.environment import validate_configuration
from app.integrations.openai.browser.signup import validate_signup_assets
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account

OWNER = 'hixz2611@gmail.com'
OLD = 'muckier_oak7w@icloud.com'
OLD_ID = 'user-IiMNiOSyZruI2FPlGzEGbmsI'
OFFICIAL_ID = '617649a2-2463-4e4f-bab8-ab145aa8a1bf'
WORKSPACE = 11
STATE = {'state': 'starting', 'workspace_id': WORKSPACE, 'owner': OWNER,
         'old_child': OLD, 'removed': False, 'pushed': False, 'stages': [], 'timeline': []}


def progress(**fields):
    STATE.update(fields, updated_at=utcnow().isoformat())
    temporary = ROOT / 'trial-state.next.json'
    temporary.write_text(json.dumps(STATE, ensure_ascii=True), encoding='utf-8')
    temporary.replace(ROOT / 'trial-state.json')


def safe_roster(rows):
    return [{key: row.get(key) for key in ('email', 'role', 'seat_type', 'id')} for row in rows]


async def main():
    settings = load_settings()
    assert settings.browser_signup_flow == 'extension' and settings.browser_engine == 'chromix'
    assert settings.database_url == 'sqlite+aiosqlite:////app/data/team48.db'
    assert str(ROOT).startswith('/app/data/experiments/managed-signup-jupiter-')
    assert json.loads((ROOT / 'smoke-passed.json').read_text())['ok']
    validate_configuration(settings)
    validate_signup_assets()
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    original_run, original_claim = InvitedBrowserSession.run, hme.maybe_claim_alias
    operation = heartbeat = None
    claims = 0
    final = {'success': False, 'error_code': 'trial_interrupted'}

    async def observed_run(self, *, on_stage=None, **kwargs):
        async def stage_callback(stage, message):
            if stage != 'heartbeat':
                STATE['stages'].append(stage)
                STATE['timeline'].append({'stage': stage, 'at': utcnow().isoformat()})
                progress(browser_stage=stage)
            if on_stage:
                pending = on_stage(stage, message)
                if inspect.isawaitable(pending):
                    await pending
        outcome = await original_run(self, on_stage=stage_callback, **kwargs)
        summary = {key: outcome.get(key) for key in ('ok', 'error_code', 'signup_flow', 'signup_version', 'signup_attempts', 'signup_retries', 'signup_diagnostics')}
        result_key = 'registration_result' if kwargs.get('_invite_onboard') else 'oauth_result'
        progress(**{result_key: summary})
        return outcome

    async def single_claim(db, email_line, **kwargs):
        nonlocal claims
        if email_line.strip() or claims:
            raise RuntimeError('fresh_hme_guard')
        claims += 1
        email, claimed = await original_claim(db, '', **kwargs)
        progress(new_child=email, hme_claimed=True)
        return email, claimed

    async def no_standby(db):
        return None

    try:
        async with factory() as db:
            workspace = await workspace_service.load_workspace(db, WORKSPACE)
            assert workspace and workspace.name == 'Jupiter 1' and workspace.status == 'active'
            assert workspace.official_workspace_id == OFFICIAL_ID
            owner = await db.get(Account, workspace.owner_account_id)
            assert owner and owner.id == 26 and owner.email == OWNER
            old_account = await db.scalar(select(Account).where(Account.email == OLD))
            assert old_account and old_account.id == 296
            cfg, cf = await hme.load_config(db), await load_cf_config(db)
            assert cfg.configured and all(cf.values()) and owner.proxy
            assert not await operation_store.browser_busy(db)
            assert not await operation_store.active_for_workspace(db, WORKSPACE)
            hme_account = hme.resolve_account(hme.hme_client.list_accounts(cfg), cfg.account_id)
            aliases = hme.hme_client.list_aliases(cfg, str(hme_account['id']))
            unavailable = await hme.active_leased_emails(db) | await hme.occupied_account_emails(db)
            assert hme.pick_next_unoccupied(aliases, unavailable) is not None
            service = OnboardService()
            service.pick_replacement = no_standby
            pending_email, preflight = await invitation_flow.prepare(service, db, workspace_id=WORKSPACE,
                email_line='', phone_line='', role='owner', seat_intent='premium', skip_invite=False)
            assert not pending_email and (not preflight or preflight.get('error_code') == 'team_full')

            async def official():
                token = decrypt_secret(owner.access_token_encrypted)
                members = await chatgpt_client.get_members(token, OFFICIAL_ID, db, identifier=OWNER)
                invites = await chatgpt_client.get_invites(token, OFFICIAL_ID, db, identifier=OWNER)
                assert members.get('success') and invites.get('success')
                return members.get('members') or [], invites.get('items') or []

            members, invites = await official()
            assert len(members) == 2 and not invites and {m.get('email') for m in members} == {OWNER, OLD}
            old = next(m for m in members if m.get('email') == OLD)
            assert old.get('id') == OLD_ID and old.get('role') == 'account-owner' and old.get('seat_type') == 'prolite'
            assert next(m for m in members if m.get('email') == OWNER).get('role') == 'account-owner'
            operation, blocker = await operation_store.create_workspace_locked(db, op_type='onboard', workspace_id=WORKSPACE,
                email=OLD, input_payload={'mode': 'approved_new_hme_replacement', 'old_email': OLD, 'signup_flow': 'extension',
                    'role': 'owner', 'seat_intent': 'premium', 'oauth_signup': True}, lease_seconds=1200)
            if blocker:
                operation = None
                raise RuntimeError('workspace_busy')
            await db.commit()
            progress(state='preflight', operation_id=operation.public_id, before_members=safe_roster(members))

            async def keep_alive():
                while True:
                    await asyncio.sleep(20)
                    async with factory() as lease_db:
                        await operation_store.heartbeat_active(lease_db, operation.public_id, lease_seconds=1200)
                        await lease_db.commit()
            heartbeat = asyncio.create_task(keep_alive())
            current, pending = await official()
            assert safe_roster(current) == safe_roster(members) and not pending
            progress(state='removing_old_child')
            removal = await workspace_service.delete_member(db, WORKSPACE, OLD_ID, email=OLD)
            if not removal.get('success'):
                final = {'success': False, 'error_code': removal.get('error_code') or 'remove_failed'}
                return
            for _ in range(5):
                members, invites = await official()
                if len(members) == 1 and members[0].get('email') == OWNER and not invites:
                    break
                await asyncio.sleep(3)
            else:
                raise RuntimeError('removal_not_confirmed_do_not_repeat')
            await record_confirmed_departure(db, workspace, OLD)
            if not await has_other_active_context(db, old_account, WORKSPACE):
                await workspace_service.mark_standby(db, old_account,
                    next_eligible_at=utcnow() + timedelta(seconds=KICK_COOLDOWN_SECONDS), workspace_id=WORKSPACE)
            await db.commit()
            progress(state='registering', removed=True)
            InvitedBrowserSession.run, hme.maybe_claim_alias = observed_run, single_claim
            final = await service.invite_and_onboard(db, workspace_id=WORKSPACE, email_line='', role='owner',
                seat_intent='premium', oauth_signup=True, phone_line='', browser_executable=settings.browser_executable,
                job_id=operation.public_id)
            await db.commit()
            members, invites = await official()
            progress(final_members=safe_roster(members), final_invites=safe_roster(invites))
            new = [m for m in members if m.get('email') == STATE.get('new_child')]
            if new:
                assert len(new) == 1 and new[0]['role'] == 'account-owner' and new[0]['seat_type'] == 'prolite'
                alias = next(a for a in hme.hme_client.list_aliases(cfg, str(hme_account['id'])) if a.get('email') == STATE['new_child'])
                tag = hme.resolve_workspace_tag(workspace, cfg.team_tag_map)
                hme.hme_client.set_local_label(cfg, str(hme_account['id']), alias['anonymousId'], tag)
                progress(hme_local_label=tag)
            if final.get('success'):
                assert len(members) == 2 and any(m.get('email') == OWNER for m in members) and not invites and new and final.get('authorized')
    except asyncio.CancelledError:
        final = {'success': False, 'error_code': 'trial_timeout'}
        raise
    except Exception as exc:
        allowed = {'workspace_busy', 'fresh_hme_guard', 'removal_not_confirmed_do_not_repeat'}
        final = {'success': False, 'error_code': str(exc) if str(exc) in allowed else type(exc).__name__}
    finally:
        InvitedBrowserSession.run, hme.maybe_claim_alias = original_run, original_claim
        if heartbeat:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
        if operation:
            async with factory() as db:
                row = await operation_store.get_by_public_id(db, operation.public_id)
                if row:
                    await operation_store.finish(db, row, final)
                    await db.commit()
        progress(state='completed' if final.get('success') else 'stopped', success=bool(final.get('success')),
            error_code=final.get('error_code'), authorized=bool(final.get('authorized')),
            new_account_id=(final.get('child') or {}).get('id'), pushed=False)
        await engine.dispose()


if __name__ == '__main__':
    if '--confirm-once' not in sys.argv:
        raise SystemExit('Explicit one-time execution required')
    os.umask(0o077)
    with (ROOT / 'trial-started.lock').open('x', encoding='utf-8') as marker:
        marker.write(utcnow().isoformat())
    try:
        asyncio.run(asyncio.wait_for(main(), timeout=1200))
    except TimeoutError:
        pass
