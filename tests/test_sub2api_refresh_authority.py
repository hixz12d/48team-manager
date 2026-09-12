"""Remote ownership uses isolated SQLite and synthetic tokens; no upstream traffic."""
import copy
import json
import unittest
from datetime import timedelta
from unittest.mock import AsyncMock, patch
from sqlalchemy import update
from app.persistence.models.identity import Account
from app.application.oauth_sessions import oauth_session_store, OAuthSessionError

import tests.test_sub2api_management as management
from tests.test_sub2api_remote_state import fixture
from app.application.sub2api_refresh_authority import preview_authority, adopt_authority, authority_state
from app.application.tokens import AuthService
from app.application.sub2api_publish import account_sub2api_push
from app.core.time import utcnow
from app.persistence.models.quota import CredentialLease
from app.persistence.models.sub2api import Sub2ApiRefreshAuthority


class RefreshAuthorityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await management.Sub2ApiManagementTests.asyncSetUp(self)
        self.snapshot = fixture()
        self.snapshot["access_token_readback"] = True
        self.full = {"id": 42, "platform": "openai", "type": "oauth", "status": "active", "schedulable": False,
                     "updated_at": self.snapshot["account_updated_at"], "credentials": {
                         "email": self.account.email, "chatgpt_account_id": "acct-billing", "workspace_id": "",
                         "_token_version": 7, "access_token": "fixture-remote-AT", "refresh_token": "fixture-remote-RT", "client_id": "fixture-client"}}
        self.source = AsyncMock(side_effect=lambda *_: {"ok": True, "snapshot": copy.deepcopy(self.snapshot)})
        self.get = AsyncMock(side_effect=lambda *_: self.readback())
        self.patches = [patch("app.application.sub2api_remote_state.request_state", new=self.source),
                        patch("app.application.sub2api_refresh_authority.request_access_token", new=self.get),
                        patch("app.application.sub2api_refresh_authority._encrypt", side_effect=lambda value: "sealed:" + value)]
        for item in self.patches: item.start()
        self.native = AsyncMock()
        self.auth = AuthService(self.native)

    async def asyncTearDown(self):
        for item in reversed(self.patches): item.stop()
        await management.Sub2ApiManagementTests.asyncTearDown(self)

    async def adopt(self):
        await self.session.refresh(self.account)
        preview = await preview_authority(self.session, self.account.id)
        self.assertTrue(preview["ok"], preview)
        return await adopt_authority(self.session, self.account.id, preview["preconditions"])

    def readback(self, full=None):
        full = full or self.full
        creds = full["credentials"]
        return {"ok": True, "remote_account_id": 42, "instance_id": self.snapshot["instance_id"],
                "credential_version": creds["_token_version"], "account_updated_at": full["updated_at"],
                "access_token": creds["access_token"], "client_id": creds["client_id"], "refresh_configured": True}

    def advance(self):
        self.snapshot["credential_version"] = 8
        self.snapshot["account_updated_at"] = "2026-09-11T12:02:00+00:00"
        self.full["updated_at"] = self.snapshot["account_updated_at"]
        self.full["credentials"].update(_token_version=8, access_token="fixture-next-AT", refresh_token="fixture-next-RT")

    async def test_preview_does_not_fetch_or_modify_tokens(self):
        result = await preview_authority(self.session, self.account.id)
        self.assertTrue(result["ok"])
        self.get.assert_not_awaited()
        self.assertEqual(self.account.access_token_encrypted, "enc-access")
        self.assertIsNone(await self.session.get(Sub2ApiRefreshAuthority, self.account.id))
        self.assertNotIn("fixture-remote", json.dumps(result))

    async def test_adoption_is_persistent_and_does_not_clear_auth_error(self):
        self.account.auth_state = "oauth_required"
        await self.session.commit()
        result = await self.adopt()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.account.access_token_encrypted, "sealed:fixture-remote-AT")
        self.assertIsNone(self.account.refresh_token_encrypted)
        self.assertIsNone(self.account.id_token_encrypted)
        self.assertEqual(self.account.auth_state, "oauth_required")
        async with self.session_maker() as fresh:
            status = await authority_state(fresh, self.account.id)
            self.assertEqual(status["owner"], "sub2api")
            self.assertEqual(status["remote_version"], 7)
            self.assertNotIn("fixture-remote", json.dumps(status))

    async def test_duplicate_confirmation_cannot_copy_twice(self):
        preview = await preview_authority(self.session, self.account.id)
        self.assertTrue((await adopt_authority(self.session, self.account.id, preview["preconditions"]))["ok"])
        revision = self.account.credential_revision
        repeated = await adopt_authority(self.session, self.account.id, preview["preconditions"])
        self.assertFalse(repeated["ok"])
        self.assertEqual(self.account.credential_revision, revision)

    async def test_common_refresh_path_pulls_new_at_without_consuming_rt(self):
        self.assertTrue((await self.adopt())["ok"])
        self.advance()
        result = await self.auth.refresh_account(self.session, self.account, schedule_checks=False)
        self.assertTrue(result["success"], result)
        self.assertTrue(result["pulled"])
        self.assertEqual(self.account.access_token_encrypted, "sealed:fixture-next-AT")
        self.assertIsNone(self.account.refresh_token_encrypted)
        self.native.refresh_access_token.assert_not_awaited()

    async def test_background_probe_receives_new_at_without_clearing_oauth_state(self):
        self.account.auth_state = "oauth_required"
        await self.session.commit()
        await self.adopt(); self.advance()
        with patch("app.application.tokens.automation_gate", new=AsyncMock(return_value={"ok": True})), \
             patch("app.application.quota.quota_service.enqueue_after_credentials", new=AsyncMock()) as enqueue:
            result = await self.auth.run_probe_once(self.session, settings={"enabled": True, "window_hours": 1, "interval_minutes": 5, "client_id": "ignored"})
        self.assertEqual(result["refreshed"], 1)
        self.assertEqual(self.account.auth_state, "oauth_required")
        enqueue.assert_awaited_once()
        self.native.refresh_access_token.assert_not_awaited()

    async def test_same_version_defers_without_fallback_or_token_read(self):
        await self.adopt(); self.get.reset_mock()
        result = await self.auth.refresh_account(self.session, self.account, schedule_checks=False)
        self.assertEqual(result["error_code"], "remote_refresh_pending")
        self.assertFalse(result["allow_oauth"])
        self.get.assert_not_awaited(); self.native.refresh_access_token.assert_not_awaited()

    async def test_local_reauthorization_blocks_automatic_remote_overwrite(self):
        await self.adopt(); self.advance(); self.get.reset_mock()
        self.account.credential_revision += 1
        self.account.access_token_encrypted = "sealed:local-new-grant"
        self.account.refresh_token_encrypted = "sealed:local-new-RT"
        await self.session.commit()
        result = await self.auth.refresh_account(self.session, self.account, schedule_checks=False)
        self.assertEqual(result["error_code"], "local_credentials_changed")
        self.assertEqual(self.account.access_token_encrypted, "sealed:local-new-grant")
        self.get.assert_not_awaited(); self.native.refresh_access_token.assert_not_awaited()

    async def test_binding_deletion_never_restores_local_refresh(self):
        await self.adopt()
        await self.session.delete(self.binding); await self.session.commit()
        result = await self.auth.refresh_account(self.session, self.account, schedule_checks=False)
        self.assertEqual(result["error_code"], "binding_changed")
        self.native.refresh_access_token.assert_not_awaited()
        self.assertIsNotNone(await self.session.get(Sub2ApiRefreshAuthority, self.account.id))

    async def test_expired_but_unresolved_local_refresh_blocks_handoff(self):
        self.session.add(CredentialLease(account_id=self.account.id, token="unresolved-local-refresh", expires_at=utcnow() - timedelta(minutes=1)))
        await self.session.commit()
        result = await self.adopt()
        self.assertEqual(result["error_code"], "refresh_in_flight")
        await self.session.refresh(self.account)
        self.assertEqual(self.account.refresh_token_encrypted, "enc-refresh")

    async def test_local_legacy_write_before_pull_is_detected_by_fingerprint(self):
        await self.adopt(); self.advance(); self.get.reset_mock()
        revision = self.account.credential_revision
        await self.session.execute(update(Account).where(Account.id == self.account.id).values(access_token_encrypted="legacy-new-local-AT"))
        self.assertEqual(self.account.credential_revision, revision)
        await self.session.commit()
        state = await authority_state(self.session, self.account.id)
        self.assertEqual(state["error_code"], "local_credentials_changed")
        result = await self.auth.refresh_account(self.session, self.account, schedule_checks=False)
        self.assertEqual(result["error_code"], "local_credentials_changed")
        self.assertEqual(self.account.access_token_encrypted, "legacy-new-local-AT")
        self.get.assert_not_awaited(); self.native.refresh_access_token.assert_not_awaited()

    async def test_preview_binds_local_fingerprint_even_without_revision_change(self):
        preview = await preview_authority(self.session, self.account.id)
        await self.session.execute(update(Account).where(Account.id == self.account.id).values(access_token_encrypted="legacy-new-local-AT"))
        await self.session.commit()
        result = await adopt_authority(self.session, self.account.id, preview["preconditions"])
        self.assertEqual(result["error_code"], "local_credentials_changed")
        self.assertEqual(self.account.access_token_encrypted, "legacy-new-local-AT")
        self.get.assert_not_awaited()

    async def persist_oauth(self, mode="auto", ticket="fixture-oauth"):
        with patch("app.application.oauth_sessions.token_cipher") as cipher:
            cipher.return_value.encrypt.return_value = "sealed:fixture-verifier"
            return await oauth_session_store.persist(self.session, {"ticket": ticket, "email": self.account.email,
                "state": "fixture-state", "code_verifier": "fixture-verifier", "mode": mode,
                "redirect_uri": "http://localhost:1455/auth/callback", "expires_at": utcnow() + timedelta(minutes=5)},
                purpose="account_reauth", account_id=self.account.id, credential_revision=self.account.credential_revision)

    async def test_active_authorization_blocks_handoff_even_expired_exchange(self):
        row = await self.persist_oauth()
        await self.session.commit()
        result = await self.adopt()
        self.assertEqual(result["error_code"], "oauth_in_flight")
        await self.session.refresh(row)
        row.status = "exchanging"
        row.expires_at = utcnow() - timedelta(minutes=1)
        await self.session.commit()
        result = await self.adopt()
        self.assertEqual(result["error_code"], "oauth_in_flight")
        await self.session.refresh(row)
        row.status = "failed"
        await self.session.commit()
        self.assertTrue((await self.adopt())["ok"])

    async def test_auto_oauth_session_creation_is_blocked_by_persistent_owner(self):
        await self.adopt()
        with self.assertRaises(OAuthSessionError) as raised:
            await self.persist_oauth()
        self.assertEqual(raised.exception.error_code, "remote_refresh_owned")
        await self.session.rollback()

    async def test_independent_auto_reauth_entrypoints_skip_owned_account(self):
        from app.application.reauth import reauth_service
        await self.adopt()
        with patch("app.application.reauth.automation_gate", new=AsyncMock()) as gate:
            queued = await reauth_service.start_auto_reauth(self.session, self.account)
            immediate = await reauth_service.run_immediate_reauth(self.session, self.account)
        for result in (queued, immediate):
            self.assertEqual(result["error_code"], "remote_refresh_owned")
            self.assertFalse(result["allow_oauth"])
        gate.assert_not_awaited()

    async def test_manual_reauthorization_is_allowed_but_pauses_background_pull(self):
        await self.adopt(); self.advance()
        await self.persist_oauth(mode="manual")
        await self.session.commit()
        result = await self.auth.refresh_account(self.session, self.account, schedule_checks=False)
        self.assertEqual(result["error_code"], "oauth_in_flight")
        self.assertEqual(self.account.access_token_encrypted, "sealed:fixture-remote-AT")
        self.native.refresh_access_token.assert_not_awaited()

    async def test_legacy_local_write_without_revision_is_not_overwritten(self):
        async def changing(*_):
            await self.session.execute(update(Account).where(Account.id == self.account.id).values(access_token_encrypted="legacy-concurrent-AT"))
            await self.session.commit()
            return self.readback()
        self.get.side_effect = changing
        result = await self.adopt()
        self.assertEqual(result["error_code"], "local_credentials_changed")
        await self.session.refresh(self.account)
        self.assertEqual(self.account.access_token_encrypted, "legacy-concurrent-AT")

    async def test_remote_change_during_secret_read_rejects_candidate(self):
        async def changing(*_):
            old = copy.deepcopy(self.full)
            self.advance()
            return self.readback(old)
        self.get.side_effect = changing
        result = await self.adopt()
        self.assertEqual(result["error_code"], "remote_changed")
        self.assertEqual(self.account.access_token_encrypted, "enc-access")

    async def test_lost_lease_cannot_store_received_access_token(self):
        await self.adopt(); self.advance()
        async def changing(*_):
            lease = await self.session.get(CredentialLease, self.account.id)
            lease.token = "new-holder"
            await self.session.commit()
            return self.readback()
        self.get.side_effect = changing
        result = await self.auth.refresh_account(self.session, self.account, schedule_checks=False)
        self.assertEqual(result["error_code"], "lease_lost")
        self.assertEqual(self.account.access_token_encrypted, "sealed:fixture-remote-AT")
        self.native.refresh_access_token.assert_not_awaited()
        lease = await self.session.get(CredentialLease, self.account.id, populate_existing=True)
        self.assertEqual(lease.token, "new-holder")

    async def test_metadata_push_cannot_echo_at_and_remove_remote_rt(self):
        await self.adopt()
        update = AsyncMock(return_value={"id": 42})
        with patch("app.application.sub2api_publish.sub2api_client.update_account", new=update), \
             patch("app.application.sub2api_publish.sub2api_client.read_after_write", new=AsyncMock(return_value=self.full)), \
             patch("app.application.sub2api_publish.sync_bound_oauth_credentials", new=AsyncMock()) as sync, \
             patch("app.application.sub2api_publish.decrypt_secret", side_effect=lambda v: "fixture-AT" if v else ""):
            await account_sub2api_push(self.session, self.account.id, name="explicit-name")
        sync.assert_not_awaited()
        self.assertEqual(update.await_args.args[2], {"name": "explicit-name"})
        self.assertEqual(self.full["credentials"]["refresh_token"], "fixture-remote-RT")
