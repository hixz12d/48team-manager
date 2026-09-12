import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from sqlalchemy import update
import tests.test_sub2api_management as management
from app.application.tokens import AuthService
from app.core.time import utcnow
from app.persistence.models.identity import Account
from app.persistence.models.quota import CredentialLease


class LocalRefreshFencingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await management.Sub2ApiManagementTests.asyncSetUp(self)
        self.account_id = self.account.id
        self.native = AsyncMock()
        self.auth = AuthService(self.native)
        self.decrypt = patch("app.application.tokens.decrypt_secret", return_value="fixture-RT")
        self.decrypt.start()

    async def asyncTearDown(self):
        self.decrypt.stop()
        await management.Sub2ApiManagementTests.asyncTearDown(self)

    async def test_unknown_result_does_not_expire_into_retry(self):
        self.native.refresh_access_token.return_value = {"success": False, "error_code": "transport"}
        first = await self.auth.refresh_account(self.session, self.account, schedule_checks=False)
        self.assertEqual(first["error_code"], "refresh_outcome_unknown")
        lease = await self.session.get(CredentialLease, self.account_id)
        self.assertTrue(lease.token)
        second = await self.auth.refresh_account(self.session, self.account, schedule_checks=False, now=utcnow()+timedelta(hours=1))
        self.assertEqual(second["error_code"], "refresh_deferred")
        self.native.refresh_access_token.assert_awaited_once()

    async def test_legacy_write_without_revision_still_defeats_late_refresh(self):
        async def race(*_args, **_kwargs):
            await self.session.execute(update(Account).where(Account.id == self.account_id).values(access_token_encrypted="new-local-auth").execution_options(synchronize_session=False))
            await self.session.commit()
            return {"success": True, "access_token": "stale-reply", "refresh_token": "stale-RT"}
        self.native.refresh_access_token.side_effect = race
        result = await self.auth.refresh_account(self.session, self.account, schedule_checks=False)
        self.assertEqual(result["error_code"], "credential_revision_conflict")
        await self.session.refresh(self.account)
        self.assertEqual(self.account.access_token_encrypted, "new-local-auth")

    async def test_replaced_lease_cannot_write_or_release_new_owner(self):
        async def race(*_args, **_kwargs):
            await self.session.execute(update(CredentialLease).where(CredentialLease.account_id == self.account_id).values(token="different-owner", expires_at=utcnow()+timedelta(minutes=3)))
            await self.session.commit()
            return {"success": True, "access_token": "stale-reply", "refresh_token": "stale-RT"}
        self.native.refresh_access_token.side_effect = race
        result = await self.auth.refresh_account(self.session, self.account, schedule_checks=False)
        self.assertEqual(result["error_code"], "refresh_outcome_unknown")
        lease = await self.session.get(CredentialLease, self.account_id, populate_existing=True)
        self.assertEqual(lease.token, "different-owner")
        await self.session.refresh(self.account)
        self.assertNotEqual(self.account.access_token_encrypted, "stale-reply")
