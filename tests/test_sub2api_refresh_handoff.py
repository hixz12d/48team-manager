import copy
import json
import unittest
from unittest.mock import AsyncMock, patch
from sqlalchemy import update
import tests.test_sub2api_refresh_authority as authority
from app.application.sub2api_refresh_handoff import preview_return, return_to_team
from app.application.sub2api_refresh_authority import authority_state
from app.application.refresh_ownership import remote_refresh_owner, remote_accepts_access_token_only
from app.persistence.models.identity import Account
from app.persistence.models.quota import CredentialLease
from app.persistence.models.refresh_handoff import Sub2ApiRefreshHandoff
from app.core.time import utcnow
from datetime import timedelta


class RefreshHandoffTests(unittest.IsolatedAsyncioTestCase):
    adopt = authority.RefreshAuthorityTests.adopt
    readback = authority.RefreshAuthorityTests.readback

    async def asyncSetUp(self):
        await authority.RefreshAuthorityTests.asyncSetUp(self)
        self.assertTrue((await self.adopt())["ok"])
        self.account_id = self.account.id
        self.account.auth_state = "oauth_required"
        await self.session.commit()
        self.calls = []
        self.waiting = False
        async def bridge(db, remote_id, request, action):
            self.calls.append((action, request["operation_id"]))
            result = {"ok": True, "state": "acknowledged" if action == "ack" else "draining" if self.waiting else "ready", "epoch": 2, "credential_version": 8}
            if action == "read":
                result["credentials"] = {"access_token": "returned-AT", "refresh_token": "returned-RT", "client_id": "fixture-client"}
            return result
        self.bridge = AsyncMock(side_effect=bridge)
        self.handoff_patches = [patch("app.application.sub2api_refresh_handoff.handoff_call", new=self.bridge),
            patch("app.application.sub2api_refresh_handoff._encrypt", side_effect=lambda v: "sealed:" + v)]
        for p in self.handoff_patches: p.start()

    async def asyncTearDown(self):
        for p in reversed(self.handoff_patches): p.stop()
        await authority.RefreshAuthorityTests.asyncTearDown(self)

    async def test_preview_is_read_only_and_return_preserves_auth_and_pause(self):
        before = self.account.operational_state
        preview = await preview_return(self.session, self.account_id)
        self.bridge.assert_not_awaited()
        result = await return_to_team(self.session, self.account_id, preview["preconditions"])
        self.assertTrue(result["ok"], result)
        await self.session.refresh(self.account)
        self.assertEqual(self.account.refresh_token_encrypted, "sealed:returned-RT")
        self.assertEqual(self.account.auth_state, "oauth_required")
        self.assertEqual(self.account.operational_state, before)
        self.assertIsNone(await remote_refresh_owner(self.session, self.account_id))
        self.assertTrue(await remote_accepts_access_token_only(self.session, 42))
        self.assertEqual((await authority_state(self.session, self.account_id))["owner"], "team")
        self.assertNotIn("returned-RT", json.dumps(result))
        row = await self.session.get(Sub2ApiRefreshHandoff, self.account_id)
        self.assertNotIn("returned-RT", row.request_json)
        with patch.object(self.auth, "_refresh_claimed", new=AsyncMock(return_value={"success": True})) as native:
            self.assertTrue((await self.auth.refresh_account(self.session, self.account, schedule_checks=False))["success"])
            native.assert_awaited_once()

    async def test_drain_retry_survives_new_session_and_uses_same_operation(self):
        self.waiting = True
        preview = await preview_return(self.session, self.account_id)
        pending = await return_to_team(self.session, self.account_id, preview["preconditions"])
        self.assertTrue(pending["pending"])
        self.assertEqual([a for a, _ in self.calls], ["prepare"])
        self.waiting = False
        async with self.session_maker() as fresh:
            preview = await preview_return(fresh, self.account_id)
            done = await return_to_team(fresh, self.account_id, preview["preconditions"])
        self.assertTrue(done["ok"], done)
        self.assertEqual(len({op for _, op in self.calls}), 1)

    async def test_changed_local_credential_after_remote_read_never_overwrites(self):
        original = self.bridge.side_effect
        async def race(db, remote_id, request, action):
            result = await original(db, remote_id, request, action)
            if action == "read":
                await db.execute(update(Account).where(Account.id == self.account_id).values(access_token_encrypted="new-local-auth"))
                await db.commit()
            return result
        self.bridge.side_effect = race
        preview = await preview_return(self.session, self.account_id)
        result = await return_to_team(self.session, self.account_id, preview["preconditions"])
        self.assertEqual(result["error_code"], "local_credentials_changed")
        await self.session.refresh(self.account)
        self.assertEqual(self.account.access_token_encrypted, "new-local-auth")
        self.assertIsNotNone(await remote_refresh_owner(self.session, self.account_id))
        self.assertNotIn("ack", [a for a, _ in self.calls])

    async def test_expired_local_attempt_still_blocks_handoff(self):
        self.session.add(CredentialLease(account_id=self.account_id, token="unknown-old-attempt", expires_at=utcnow()-timedelta(hours=1)))
        await self.session.commit()
        preview = await preview_return(self.session, self.account_id)
        result = await return_to_team(self.session, self.account_id, preview["preconditions"])
        self.assertEqual(result["error_code"], "refresh_in_flight")
        self.bridge.assert_not_awaited()

    async def test_ack_failure_keeps_team_owner_and_retries_cleanup_only(self):
        original = self.bridge.side_effect
        async def fail_ack(db, remote_id, request, action):
            if action == "ack":
                return {"ok": False, "error_code": "handoff_unavailable"}
            return await original(db, remote_id, request, action)
        self.bridge.side_effect = fail_ack
        preview = await preview_return(self.session, self.account_id)
        result = await return_to_team(self.session, self.account_id, preview["preconditions"])
        self.assertTrue(result["ok"])
        self.assertTrue(result["partial"])
        self.assertIsNone(await remote_refresh_owner(self.session, self.account_id))
        self.assertFalse((await authority_state(self.session, self.account_id))["handoff_acknowledged"])
        reads = len([a for a, _ in self.calls if a == "read"])
        self.bridge.side_effect = original
        preview = await preview_return(self.session, self.account_id)
        final = await return_to_team(self.session, self.account_id, preview["preconditions"])
        self.assertTrue(final["acknowledged"])
        self.assertEqual(reads, len([a for a, _ in self.calls if a == "read"]))
        self.assertTrue((await authority_state(self.session, self.account_id))["handoff_acknowledged"])

    async def test_failed_read_is_resumable_without_switching_owner(self):
        original = self.bridge.side_effect
        async def fail(db, remote_id, request, action):
            if action == "read":
                return {"ok": False, "error_code": "handoff_unavailable"}
            return await original(db, remote_id, request, action)
        self.bridge.side_effect = fail
        preview = await preview_return(self.session, self.account_id)
        result = await return_to_team(self.session, self.account_id, preview["preconditions"])
        self.assertFalse(result["ok"])
        self.assertIsNotNone(await remote_refresh_owner(self.session, self.account_id))
        self.assertEqual((await self.session.get(Sub2ApiRefreshHandoff, self.account_id)).state, "pending")
