"""One approved Lava2 replacement; invite only, never launch signup or OAuth."""
import asyncio
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import sqlite3

logging.disable(logging.CRITICAL)
from app.application.onboard import OnboardService
from sqlalchemy import select
from app.application.identity import ensure_membership
from app.application.member_lifecycle import record_confirmed_departure, has_other_active_context
from app.application.operations import operation_store
from app.application.resources import hme
from app.application.tokens import decrypt_secret
from app.application.workspaces import workspace_service
from app.core.config import load_settings
from app.core.time import utcnow
from app.domain.onboard import KICK_COOLDOWN_SECONDS
from app.integrations.openai.chatgpt import chatgpt_client
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account

ROOT = Path(__file__).resolve().parent
OWNER = 'jeromezdeanf2r2w@gmail.com'
OLD = 'lookups-95-blaring@icloud.com'
OLD_USER = 'user-ICgJekMcJE8c7z5NiicUVpIJ'
OFFICIAL = '7e3d7c37-6f96-4ec1-b1b9-74ede0255423'
STATE = {'workspace_id': 19, 'workspace': 'Lava2', 'owner': OWNER, 'old_child': OLD,
         'removed': False, 'invited': False, 'signup_started': False}


def save(**fields):
    STATE.update(fields, updated_at=utcnow().isoformat())
    temp = ROOT / 'state.next.json'
    temp.write_text(json.dumps(STATE), encoding='utf-8')
    temp.replace(ROOT / 'state.json')


def roster(rows):
    return [{'email': r.get('email') or r.get('email_address'),
             **{k: r.get(k) for k in ('id', 'role', 'seat_type')}} for r in rows]


async def main():
    settings = load_settings()
    assert settings.database_url == 'sqlite+aiosqlite:////app/data/team48.db'
    assert str(ROOT).startswith('/app/data/experiments/lava2-invite-only-')
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    operation = claimed = None
    invite_attempted = False
    final = {'success': False, 'error_code': 'interrupted'}
    try:
        async with factory() as db:
            ws = await workspace_service.load_workspace(db, 19)
            assert ws and ws.name == 'Lava2' and ws.status == 'active' and ws.official_workspace_id == OFFICIAL
            owner = await db.get(Account, ws.owner_account_id)
            old = await db.get(Account, 293)
            assert owner.id == 151 and owner.email == OWNER and old.email == OLD
            assert not await operation_store.active_for_workspace(db, 19)
            cfg = await hme.load_config(db)
            assert cfg.configured

            async def official():
                token = decrypt_secret(owner.access_token_encrypted)
                members = await chatgpt_client.get_members(token, OFFICIAL, db, identifier=OWNER)
                invites = await chatgpt_client.get_invites(token, OFFICIAL, db, identifier=OWNER)
                assert members.get('success') and invites.get('success')
                return roster(members.get('members') or []), roster(invites.get('items') or [])

            members, invites = await official()
            assert len(members) == 2 and not invites and {m['email'] for m in members} == {OWNER, OLD}
            target = next(m for m in members if m['email'] == OLD)
            assert target['id'] == OLD_USER and target['role'] == 'account-owner' and target['seat_type'] == 'prolite'
            assert next(m for m in members if m['email'] == OWNER)['role'] == 'account-owner'
            operation, blocker = await operation_store.create_workspace_locked(db, op_type='onboard', workspace_id=19,
                email=OLD, input_payload={'mode': 'approved_replacement_invite_only', 'old_email': OLD,
                    'role': 'owner', 'seat_intent': 'premium', 'signup_started': False}, lease_seconds=600)
            if blocker:
                operation = None
                raise RuntimeError('workspace_busy')
            await db.commit()
            save(state='reserving_alias', operation_id=operation.public_id, before_members=members)
            claimed = await hme.claim_next_alias(db, job_id=operation.public_id, purpose='onboard', workspace_id=19)
            assert not await db.scalar(select(Account).where(Account.email == claimed.email))
            save(new_email=claimed.email)
            current, pending = await official()
            assert current == members and not pending
            save(state='removing_old_child')
            removal = await workspace_service.delete_member(db, 19, OLD_USER, email=OLD)
            assert removal.get('success')
            for _ in range(5):
                members, invites = await official()
                if len(members) == 1 and members[0]['email'] == OWNER and not invites:
                    break
                await asyncio.sleep(2)
            else:
                raise RuntimeError('removal_not_confirmed')
            await record_confirmed_departure(db, ws, OLD)
            if not await has_other_active_context(db, old, 19):
                await workspace_service.mark_standby(db, old, workspace_id=19,
                    next_eligible_at=utcnow() + timedelta(seconds=KICK_COOLDOWN_SECONDS))
            child = await OnboardService()._upsert_child(db, email=claimed.email, mail_raw=claimed.email,
                proxy=owner.proxy, proxy_source=owner.proxy_source or 'legacy', sub2api_proxy_id=owner.sub2api_proxy_id,
                proxy_instance_key=owner.proxy_instance_key or '')
            child.auth_state = 'oauth_required'
            operation.email, operation.account_id = claimed.email, child.id
            await db.commit()
            save(state='sending_invitation', removed=True, new_account_id=child.id)
            invite_attempted = True
            sent = await workspace_service.invite_member(db, 19, claimed.email, role='owner', seat_intent='premium')
            members, invites = await official()
            selected = [i for i in invites if i['email'] == claimed.email]
            assert len(members) == 1 and members[0]['email'] == OWNER and len(invites) == len(selected) == 1
            assert selected[0]['role'] == 'account-owner' and selected[0]['seat_type'] == 'prolite'
            await ensure_membership(db, workspace_id=19, account_id=child.id, official_role='owner',
                membership_state='invited', local_purpose='child')
            await db.commit()
            await hme.finalize_claim(db, claimed, {'success': True}, label='Lava2')
            data = hme.hme_client._request('GET', cfg, '/api/aliases', params={'account_id': claimed.account_id, 'q': claimed.email})
            labels = [{k: a.get(k) for k in ('email', 'label', 'active')} for a in data.get('aliases', []) if a.get('email') == claimed.email]
            assert len(labels) == 1 and labels[0]['label'] == 'Lava2'
            save(invited=True, final_members=members, final_invites=invites, hme_labels=labels,
                invite_api_success=bool(sent.get('success')))
            final = {'success': True, 'status': 'invited', 'message': '邀请已发送，等待用户在 HubStudio 注册',
                     'email': claimed.email, 'account_id': child.id, 'signup_started': False}
    except Exception as exc:
        final = {'success': False, 'error_code': type(exc).__name__}
        save(failure_type=type(exc).__name__)
    finally:
        async with factory() as db:
            if claimed and not final.get('success'):
                if invite_attempted:
                    # Preserve this exact alias after any ambiguous invitation response.
                    await hme.finalize_claim(db, claimed, {'success': True}, label='Lava2')
                else:
                    await hme.release_lease(db, claimed)
            if operation:
                row = await operation_store.get_by_public_id(db, operation.public_id)
                await operation_store.finish(db, row, final)
                await db.commit()
        save(state='completed' if final.get('success') else 'stopped', success=bool(final.get('success')),
             error_code=final.get('error_code'))
        await engine.dispose()


if __name__ == '__main__':
    os.umask(0o077)
    with (ROOT / 'started.lock').open('x') as marker:
        marker.write(utcnow().isoformat())
    with sqlite3.connect('file:/app/data/team48.db?mode=ro', uri=True) as live, sqlite3.connect(ROOT / 'before-team48.db') as backup:
        live.backup(backup)
    asyncio.run(main())
