import copy
import json
import unittest
from unittest.mock import AsyncMock, patch

import httpx
import tests.test_sub2api_sync_contract as contract
from tests.test_sub2api_sync_contract import CAPABILITIES, RECEIPT
import tests.test_sub2api_refresh_authority as authority
from app.application.sub2api_auth_recovery import preview_auth_recovery, recover_auth


class AuthRecoveryContractTests(unittest.IsolatedAsyncioTestCase):
    run_sync = contract.CredentialSyncContractTests.run_sync

    def capabilities(self):
        result = copy.deepcopy(CAPABILITIES)
        result["oauth_sync"].update(revision=4, auth_only=True, candidate_validation=True, versioned_auth_errors=True,
            validation_scope="codex_identity_usage_catalog", recovery_modes=["credentials_only", "auth_only"])
        return result

    async def test_verified_receipt_preserves_paused_state(self):
        def respond(request):
            if request.url.path.endswith("capabilities"):
                return httpx.Response(200, json={"code": 0, "data": self.capabilities()})
            self.assertEqual(json.loads(request.content)["recovery_mode"], "auth_only")
            return httpx.Response(200, json={"code": 0, "data": {**RECEIPT, "auth_recovery": "cleared", "validation_scope": "codex_identity_usage_catalog",
                "validated_at": "2026-09-12T00:00:00Z", "schedulable": False, "remaining_blockers": ["schedulable_off"]}})
        result, calls = await self.run_sync(respond, recovery_mode="auth_only")
        self.assertEqual(result["auth_recovery"], "cleared")
        self.assertFalse(result["ok"])
        self.assertEqual(result["remaining_blockers"], ["schedulable_off"])
        self.assertEqual([r.method for r in calls], ["GET", "POST"])

    async def test_forged_success_without_validation_is_unknown(self):
        def respond(request):
            return httpx.Response(200, json={"code": 0, "data": self.capabilities() if request.url.path.endswith("capabilities") else {**RECEIPT, "auth_recovery": "cleared"}})
        result, calls = await self.run_sync(respond, recovery_mode="auth_only")
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(sum(r.method == "POST" for r in calls), 1)

    async def test_validation_and_attribution_failures_are_sanitized_and_not_retried(self):
        for status, reason, code in [(400, "OAUTH_VALIDATION_FAILED", "candidate_validation_failed"), (409, "AUTH_ERROR_UNATTRIBUTED", "auth_error_unattributed")]:
            def respond(request):
                if request.method == "GET":
                    return httpx.Response(200, json={"code": 0, "data": self.capabilities()})
                return httpx.Response(status, json={"reason": reason, "message": "fixture-secret"})
            result, calls = await self.run_sync(respond, recovery_mode="auth_only")
            self.assertEqual(result["error_code"], code)
            self.assertEqual(result["credential_write"], "not_attempted")
            self.assertNotIn("fixture-secret", json.dumps(result))
            self.assertEqual(len(calls), 2)


class AuthRecoveryActionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await authority.RefreshAuthorityTests.asyncSetUp(self)
        self.account.client_id = "fixture-client"
        self.account.auth_state = "oauth_required"
        await self.session.commit()

    async def asyncTearDown(self):
        await authority.RefreshAuthorityTests.asyncTearDown(self)

    async def test_preview_never_submits_credentials(self):
        with patch("app.application.sub2api_auth_recovery.sub2api_client.sync_oauth_credentials", new=AsyncMock()) as sync:
            preview = await preview_auth_recovery(self.session, self.account.id)
        self.assertTrue(preview["ok"])
        sync.assert_not_awaited()
        self.get.assert_not_awaited()

    async def test_explicit_recovery_does_not_clear_local_auth_or_pause(self):
        preview = await preview_auth_recovery(self.session, self.account.id)
        before = self.account.operational_state
        receipt = {"ok": False, "credential_write": "succeeded", "auth_recovery": "cleared", "validation_scope": "codex_identity_usage_catalog", "remaining_blockers": ["schedulable_off"]}
        with patch("app.application.sub2api_auth_recovery._build_credentials", return_value={"access_token": "fixture-AT", "refresh_token": "fixture-RT", "client_id": "fixture-client"}), \
             patch("app.application.sub2api_auth_recovery.sub2api_client.sync_oauth_credentials", new=AsyncMock(return_value=receipt)) as sync:
            result = await recover_auth(self.session, self.account.id, preview["preconditions"])
        self.assertEqual(sync.await_args.kwargs["recovery_mode"], "auth_only")
        self.assertEqual(self.account.auth_state, "oauth_required")
        self.assertEqual(self.account.operational_state, before)
        self.assertEqual(result["auth_recovery"], "cleared")
        self.assertNotIn("fixture-AT", json.dumps(result))

    async def test_changed_local_credential_blocks_old_preview(self):
        preview = await preview_auth_recovery(self.session, self.account.id)
        self.account.access_token_encrypted = "local-newer"
        await self.session.commit()
        with patch("app.application.sub2api_auth_recovery.sub2api_client.sync_oauth_credentials", new=AsyncMock()) as sync:
            result = await recover_auth(self.session, self.account.id, preview["preconditions"])
        self.assertEqual(result["error_code"], "local_credentials_changed")
        sync.assert_not_awaited()
