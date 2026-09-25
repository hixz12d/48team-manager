"""Read-only official/local verification of the isolated Bybit2 replacement."""
import asyncio
import json
import logging
from pathlib import Path

logging.disable(logging.CRITICAL)
from sqlalchemy import select
from app.application.onboard import OnboardService
from app.application.resources import hme
from app.application.tokens import decrypt_secret
from app.core.config import load_settings
from app.persistence.database import create_engine, create_session_factory
from app.persistence.models.identity import Account, Workspace, WorkspaceMembership, ExternalBinding
from app.persistence.models.oauth import OAuthSession
from app.persistence.models.operations import Operation
from app.persistence.models.resources import HmeAliasLease
from app.integrations.openai.chatgpt import chatgpt_client


async def main():
    root = Path.cwd()
    state = json.loads((root / 'trial-state.json').read_text())
    engine = create_engine(load_settings())
    report = {'state': state}
    try:
        async with create_session_factory(engine)() as db:
            workspace = await db.get(Workspace, 17)
            owner = await db.get(Account, workspace.owner_account_id)
            assert owner.email == 'hixz2616@gmail.com' and workspace.name == 'Bybit2'
            access = decrypt_secret(owner.access_token_encrypted)
            for kind, method, key in [('members', chatgpt_client.get_members, 'members'), ('invites', chatgpt_client.get_invites, 'items')]:
                result = await method(access, workspace.official_workspace_id, db, identifier=owner.email)
                report[kind] = {'success': bool(result.get('success')), 'items': [
                    {**{field: row.get(field) for field in ('role', 'seat_type', 'id')}, 'email': row.get('email') or row.get('email_address')} for row in result.get(key) or []]}
            report['accounts'] = []
            for email in [owner.email, state['old_child'], state.get('new_child')]:
                if not email:
                    continue
                account = await db.scalar(select(Account).where(Account.email == email))
                if not account:
                    continue
                membership = await db.scalar(select(WorkspaceMembership).where(WorkspaceMembership.workspace_id == 17, WorkspaceMembership.account_id == account.id))
                report['accounts'].append({'id': account.id, 'email': account.email, 'auth_state': account.auth_state,
                    'operational_state': account.operational_state, 'local_purpose': account.local_purpose,
                    'has_access_token': bool(decrypt_secret(account.access_token_encrypted)),
                    'has_refresh_token': bool(decrypt_secret(account.refresh_token_encrypted)),
                    'has_password': bool(decrypt_secret(account.password_encrypted)), 'phone_saved': bool(account.phone),
                    'membership_state': membership.membership_state if membership else None,
                    'official_role': membership.official_role if membership else None})
            operation = await db.scalar(select(Operation).where(Operation.public_id == state['operation_id']))
            report['operation'] = {key: getattr(operation, key) for key in ('public_id', 'state', 'current_step', 'error_code', 'account_id')}
            oauth = list((await db.execute(select(OAuthSession).where(OAuthSession.operation_id == state['operation_id']))).scalars())
            report['oauth_sessions'] = [{'status': row.status, 'email': row.email, 'consumed': row.consumed_at is not None} for row in oauth]
            cfg = await hme.load_config(db)
            hme_account = hme.resolve_account(hme.hme_client.list_accounts(cfg), cfg.account_id)
            aliases = hme.hme_client.list_aliases(cfg, str(hme_account['id']))
            report['hme_labels'] = [{key: item.get(key) for key in ('email', 'label', 'active')} for item in aliases if item.get('email') == state.get('new_child')]
            leases = list((await db.execute(select(HmeAliasLease).where(HmeAliasLease.email == state.get('new_child')))).scalars())
            report['hme_leases'] = [{'state': row.local_state, 'label_sync_pending': row.label_sync_pending} for row in leases]
            bindings = list((await db.execute(select(ExternalBinding).where(ExternalBinding.local_account_id == operation.account_id))).scalars())
            report['new_account_external_bindings'] = len(bindings)
            await db.rollback()
    finally:
        await engine.dispose()
    processes = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            args = (entry / 'cmdline').read_bytes().split(b'\0')
            if any(str(root).encode() in arg for arg in args) and (b'chrome' in args[0] or any(b'team48_bybit2_trial.py' in arg or b'team48_bybit2_continue.py' in arg for arg in args)):
                processes.append({'pid': int(entry.name), 'name': Path(args[0].decode()).name})
        except (OSError, IndexError, UnicodeError):
            pass
    report['trial_processes'] = processes
    (root / 'verified-report.json').write_text(json.dumps(report, ensure_ascii=True), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=True))


if __name__ == '__main__':
    asyncio.run(main())
