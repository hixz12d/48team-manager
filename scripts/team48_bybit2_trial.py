"""One explicitly authorized Bybit2 replacement, with exact identity guards.

Run only from the isolated experiment root. Never calls rotate or Sub2API.
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

from sqlalchemy import select
from app.application.onboard import OnboardService
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

OWNER = 'hixz2616@gmail.com'
OLD = 'are.tapirs_1u@icloud.com'
OLD_ID = 'user-PqfWeyncx4yLDgnAu52irMKE'
OFFICIAL_ID = '50178e5e-2a3c-43e3-ba15-55f5d8e0ed32'
WORKSPACE = 17
STATE = {'state': 'starting', 'workspace_id': WORKSPACE, 'owner': OWNER, 'old_child': OLD,
         'removed': False, 'pushed': False, 'stages': []}


def progress(**fields):
    STATE.update(fields, updated_at=utcnow().isoformat())
    target = ROOT / 'trial-state.json'
    temporary = target.with_suffix('.next.json')
    temporary.write_text(json.dumps(STATE, ensure_ascii=True), encoding='utf-8')
    temporary.replace(target)


def safe_roster(rows):
    return [{key: row.get(key) for key in ('email', 'role', 'seat_type', 'id')} for row in rows]


async def main():
    settings = load_settings()
    assert settings.browser_signup_flow == 'extension' and settings.browser_engine == 'chromix'
    assert settings.database_url == 'sqlite+aiosqlite:////app/data/team48.db'
    assert str(ROOT).startswith('/app/data/experiments/managed-signup-bybit2-')
    validate_configuration(settings)
    validate_signup_assets()
    factory_engine = create_engine(settings)
    factory = create_session_factory(factory_engine)
    heartbeat = operation = None
    final = {'success': False, 'error_code': 'trial_interrupted'}
    original_run = InvitedBrowserSession.run
    original_claim = hme.maybe_claim_alias
    claims = 0

    async def observed_run(self, *, on_stage=None, **kwargs):
        async def stage_callback(stage, message):
            if stage != 'heartbeat':
                STATE['stages'].append(stage)
                STATE['stages'] = STATE['stages'][-150:]
                progress(browser_stage=stage)
            if on_stage:
                value = on_stage(stage, message)
                if inspect.isawaitable(value):
                    await value
        outcome = await original_run(self, on_stage=stage_callback, **kwargs)
        summary = {key: outcome.get(key) for key in ('ok', 'error_code', 'signup_flow', 'signup_version', 'signup_attempts', 'signup_retries', 'signup_diagnostics')}
        result_key = 'registration_result' if kwargs.get('_invite_onboard') else 'oauth_result'
        progress(browser_result=summary, **{result_key: summary})
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
        return None  # User explicitly requested a fresh HME, not a standby account.

    try:
        async with factory() as db:
            workspace = await workspace_service.load_workspace(db, WORKSPACE)
            if not workspace or workspace.name != 'Bybit2' or workspace.status != 'active' or workspace.official_workspace_id != OFFICIAL_ID:
                raise RuntimeError('workspace_identity_changed')
            owner = await db.get(Account, workspace.owner_account_id)
            if not owner or owner.email != OWNER:
                raise RuntimeError('mother_identity_changed')
            cf, cfg = await load_cf_config(db), await hme.load_config(db)
            if not all(cf.values()) or not cfg.configured or not owner.proxy:
                raise RuntimeError('configuration_missing')
            if await operation_store.browser_busy(db):
                raise RuntimeError('browser_busy')
            aliases = hme.hme_client.list_aliases(cfg, str(hme.resolve_account(hme.hme_client.list_accounts(cfg), cfg.account_id)['id']))
            unavailable = await hme.active_leased_emails(db) | await hme.occupied_account_emails(db)
            if hme.pick_next_unoccupied(aliases, unavailable) is None:
                raise RuntimeError('hme_empty')

            async def official():
                access = decrypt_secret(owner.access_token_encrypted)
                members = await chatgpt_client.get_members(access, OFFICIAL_ID, db, identifier=OWNER)
                invites = await chatgpt_client.get_invites(access, OFFICIAL_ID, db, identifier=OWNER)
                if not members.get('success') or not invites.get('success'):
                    raise RuntimeError('official_read_failed')
                return members.get('members') or [], invites.get('items') or []

            members, invites = await official()
            if len(members) != 2 or invites or {m.get('email') for m in members} != {OWNER, OLD}:
                raise RuntimeError('official_roster_changed')
            old = next(m for m in members if m.get('email') == OLD)
            mother = next(m for m in members if m.get('email') == OWNER)
            if old.get('id') != OLD_ID or old.get('role') != 'account-owner' or old.get('seat_type') != 'prolite' or mother.get('role') != 'account-owner':
                raise RuntimeError('official_identity_or_seat_changed')
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
            # An exact official readback immediately precedes the destructive action.
            current, pending = await official()
            if safe_roster(current) != safe_roster(members) or pending:
                raise RuntimeError('official_roster_changed')
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
            old_account = await db.scalar(select(Account).where(Account.email == OLD))
            if old_account and not await has_other_active_context(db, old_account, WORKSPACE):
                await workspace_service.mark_standby(db, old_account,
                    next_eligible_at=utcnow() + timedelta(seconds=KICK_COOLDOWN_SECONDS), workspace_id=WORKSPACE)
            await db.commit()
            progress(state='registering', removed=True)
            service = OnboardService()
            service.pick_replacement = no_standby
            InvitedBrowserSession.run = observed_run
            hme.maybe_claim_alias = single_claim
            final = await service.invite_and_onboard(db, workspace_id=WORKSPACE, email_line='',
                role='owner', seat_intent='premium', oauth_signup=True, phone_line='',
                browser_executable=settings.browser_executable, job_id=operation.public_id)
            await db.commit()
            members, invites = await official()
            progress(final_members=safe_roster(members), final_invites=safe_roster(invites))
            if final.get('success'):
                new = [m for m in members if m.get('email') == STATE.get('new_child')]
                if len(members) != 2 or not any(m.get('email') == OWNER for m in members) or invites or len(new) != 1 or new[0].get('role') != 'account-owner' or new[0].get('seat_type') != 'prolite' or not final.get('authorized'):
                    final = {**final, 'success': False, 'error_code': 'final_verification_failed'}
    except asyncio.CancelledError:
        final = {'success': False, 'error_code': 'trial_timeout'}
        raise
    except Exception as exc:
        known = {'workspace_identity_changed', 'mother_identity_changed', 'configuration_missing', 'browser_busy', 'hme_empty',
                 'official_read_failed', 'workspace_busy', 'official_roster_changed', 'official_identity_or_seat_changed',
                 'removal_not_confirmed_do_not_repeat', 'fresh_hme_guard'}
        final = {'success': False, 'error_code': str(exc) if str(exc) in known else type(exc).__name__}
    finally:
        InvitedBrowserSession.run = original_run
        hme.maybe_claim_alias = original_claim
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
        await factory_engine.dispose()


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
