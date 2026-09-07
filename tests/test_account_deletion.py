import unittest
from datetime import timedelta

from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.application.account_deletion import AccountDeletionError, delete_unassigned_account, delete_unassigned_accounts
from app.core.time import utcnow
from app.persistence.migrations.bootstrap import bootstrap_schema
from app.persistence.models.identity import Account, ExternalBinding, Workspace, WorkspaceMembership, WorkspaceOfficialMemberSnapshot
from app.persistence.models.operations import Operation
from app.persistence.models.oauth import OAuthSession
from app.persistence.models.sub2api import Sub2ApiUsageSnapshot
from app.persistence.models.quota import CredentialLease, QuotaProbeState, QuotaSnapshot
from app.persistence.models.resources import HmeAliasLease


class AccountDeletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_async_engine('sqlite+aiosqlite:///:memory:')
        @event.listens_for(self.engine.sync_engine, 'connect')
        def foreign_keys(connection, _):
            connection.execute('PRAGMA foreign_keys=ON')
        await bootstrap_schema(self.engine)
        self.db = async_sessionmaker(self.engine, expire_on_commit=False)()
        self.account = Account(email='unused@example.com', local_purpose='standby', access_token_encrypted='test-only')
        self.other = Account(email='other@example.com', local_purpose='standby')
        self.db.add_all([self.account, self.other])
        await self.db.commit()
        self.account_id = self.account.id

    async def asyncTearDown(self):
        await self.db.close()
        await self.engine.dispose()

    async def reject(self, code):
        with self.assertRaises(AccountDeletionError) as error:
            await delete_unassigned_account(self.db, self.account_id)
        self.assertEqual(error.exception.code, code)
        await self.db.rollback()
        self.assertIsNotNone(await self.db.get(Account, self.account_id))

    async def test_local_delete_keeps_other_account_audit_and_hme_occupancy(self):
        self.db.add_all([
            ExternalBinding(provider='sub2api', local_account_id=self.account_id, remote_account_id='123', binding_state='verified'),
            QuotaSnapshot(account_id=self.account_id, queried_at=utcnow(), success=True),
            QuotaProbeState(context_key=f'{self.account_id}:none', account_id=self.account_id),
            CredentialLease(account_id=self.account_id),
            HmeAliasLease(email=self.account.email, anonymous_id='alias', account_id='icloud-account', expires_at=utcnow(), local_state='used'),
            Operation(public_id='old-operation', op_type='reauth', account_id=self.account_id, entity_type='account', entity_id=self.account_id, email=self.account.email, state='failed'),
        ])
        await self.db.commit()
        binding = await self.db.scalar(select(ExternalBinding))
        self.db.add(Sub2ApiUsageSnapshot(binding_id=binding.id, local_account_id=self.account_id, remote_account_id='123', window_kind='7d'))
        await self.db.commit()
        result = await delete_unassigned_account(self.db, self.account_id)
        await self.db.commit()
        self.assertTrue(result['ok'])
        self.assertEqual(result['email'], 'unused@example.com')
        self.assertIsNone(await self.db.get(Account, self.account_id))
        self.assertIsNotNone(await self.db.get(Account, self.other.id))
        for model in (ExternalBinding, Sub2ApiUsageSnapshot, QuotaSnapshot, QuotaProbeState, CredentialLease):
            self.assertEqual(list(await self.db.scalars(select(model))), [])
        self.assertEqual(len(list(await self.db.scalars(select(HmeAliasLease)))), 1)
        op = await self.db.scalar(select(Operation))
        self.assertIsNone(op.account_id)
        self.assertIsNone(op.entity_id)
        self.assertEqual(op.email, 'unused@example.com')
        self.assertEqual(list(await self.db.execute(text('PRAGMA foreign_key_check'))), [])

    async def test_mother_is_protected(self):
        self.account.local_purpose = 'mother'
        await self.db.commit()
        await self.reject('account_is_owner')

    async def test_workspace_owner_is_protected_even_with_standby_purpose(self):
        self.db.add(Workspace(owner_account_id=self.account_id))
        await self.db.commit()
        await self.reject('account_is_owner')

    async def test_membership_blocks_deletion(self):
        workspace = Workspace(owner_account_id=self.other.id)
        self.db.add(workspace)
        await self.db.flush()
        self.db.add(WorkspaceMembership(workspace_id=workspace.id, account_id=self.account_id, membership_state='invited', local_purpose='child'))
        await self.db.commit()
        await self.reject('account_has_workspace')

    async def test_official_snapshot_blocks_deletion_without_local_membership(self):
        workspace = Workspace(owner_account_id=self.other.id)
        self.db.add(workspace)
        await self.db.flush()
        self.db.add(WorkspaceOfficialMemberSnapshot(workspace_id=workspace.id, normalized_email=self.account.email, remote_state='joined', fetched_at=utcnow()))
        await self.db.commit()
        await self.reject('account_has_workspace')

    async def test_active_operation_by_email_blocks(self):
        self.db.add(Operation(public_id='busy', op_type='onboard', email=self.account.email, state='running'))
        await self.db.commit()
        await self.reject('account_busy')

    async def test_credential_lease_blocks(self):
        self.db.add(CredentialLease(account_id=self.account_id, expires_at=utcnow()+timedelta(minutes=1)))
        await self.db.commit()
        await self.reject('account_busy')

    async def test_removed_membership_can_be_deleted(self):
        workspace = Workspace(owner_account_id=self.other.id)
        self.db.add(workspace)
        await self.db.flush()
        self.db.add(WorkspaceMembership(workspace_id=workspace.id, account_id=self.account_id, membership_state='removed', local_purpose='child'))
        await self.db.commit()
        await delete_unassigned_account(self.db, self.account_id)
        await self.db.commit()
        self.assertIsNotNone(await self.db.get(Workspace, workspace.id))
        self.assertEqual(list(await self.db.scalars(select(WorkspaceMembership))), [])

    async def test_pending_oauth_blocks_and_expired_session_is_removed(self):
        self.db.add(OAuthSession(public_id='oauth-delete-test', account_id=self.account_id,
            email=self.account.email, purpose='reauth', mode='manual', state_hash='test',
            code_verifier_encrypted='test', client_id='test', redirect_uri='http://localhost',
            authorize_url='https://example.com', expires_at=utcnow()+timedelta(minutes=5)))
        await self.db.commit()
        await self.reject('account_busy')
        oauth = await self.db.scalar(select(OAuthSession))
        oauth.expires_at = utcnow()-timedelta(minutes=1)
        await self.db.commit()
        await delete_unassigned_account(self.db, self.account_id)
        await self.db.commit()
        self.assertEqual(list(await self.db.scalars(select(OAuthSession))), [])

    async def test_missing_account_is_404(self):
        with self.assertRaises(AccountDeletionError) as error:
            await delete_unassigned_account(self.db, 9999)
        self.assertEqual(error.exception.status, 404)

    async def test_batch_deletes_eligible_and_keeps_blocked(self):
        blocked = Account(email='blocked@example.com', local_purpose='mother')
        extra = Account(email='extra@example.com', local_purpose='standby')
        self.db.add_all([blocked, extra])
        await self.db.commit()
        extra_id, blocked_id, other_id = extra.id, blocked.id, self.other.id
        result = await delete_unassigned_accounts(self.db, [self.account_id, extra_id, blocked_id, extra_id])
        self.assertTrue(result['partial'])
        self.assertEqual({item['email'] for item in result['deleted']}, {'unused@example.com', 'extra@example.com'})
        self.assertEqual(result['failed'][0]['error_code'], 'account_is_owner')
        self.assertIsNone(await self.db.get(Account, self.account_id))
        self.assertIsNone(await self.db.get(Account, extra_id))
        self.assertIsNotNone(await self.db.get(Account, blocked_id))
        self.assertIsNotNone(await self.db.get(Account, other_id))


class AccountDeletionApiTests(unittest.TestCase):
    def test_auth_confirmation_and_delete(self):
        import tempfile
        from pathlib import Path
        from tests.helpers import make_client
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            self.assertEqual(client.request('DELETE', '/api/accounts/1', json={'confirm': True}).status_code, 401)
            client.post('/auth/login', json={'username':'hixz12','password':'test-password'})
            created = client.post('/api/accounts', json={'email':'unused@example.com','purpose':'standby'})
            self.assertEqual(created.status_code, 201)
            account_id = created.json()['account']['id']
            url = f'/api/accounts/{account_id}'
            self.assertEqual(client.request('DELETE', url, json={}).status_code, 422)
            self.assertEqual(client.request('DELETE', url, json={'confirm': False}).status_code, 422)
            self.assertEqual(client.request('DELETE', url, json={'confirm': True}).status_code, 200)
            self.assertEqual(client.request('DELETE', url, json={'confirm': True}).status_code, 404)
            self.assertEqual(client.get('/api/accounts/portfolio').json()['unassigned'], [])

    def test_batch_delete_requires_confirm_and_skips_blocked(self):
        import tempfile
        from pathlib import Path
        from tests.helpers import make_client
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            self.assertEqual(client.post('/api/accounts/delete-local', json={'confirm': True, 'account_ids': [1]}).status_code, 401)
            client.post('/auth/login', json={'username':'hixz12','password':'test-password'})
            first = client.post('/api/accounts', json={'email':'one@example.com','purpose':'standby'}).json()['account']['id']
            second = client.post('/api/accounts', json={'email':'two@example.com','purpose':'standby'}).json()['account']['id']
            self.assertEqual(client.post('/api/accounts/delete-local', json={'account_ids': [first]}).status_code, 422)
            result = client.post('/api/accounts/delete-local', json={'confirm': True, 'account_ids': [first, second, 9999]})
            self.assertEqual(result.status_code, 200)
            body = result.json()
            self.assertTrue(body['partial'])
            self.assertEqual(len(body['deleted']), 2)
            self.assertEqual(body['failed'][0]['error_code'], 'not_found')
            self.assertEqual(client.get('/api/accounts/portfolio').json()['unassigned'], [])
