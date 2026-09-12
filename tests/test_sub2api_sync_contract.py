"""Wire-level sync tests. All HTTP responses and credentials are synthetic."""
import json
import unittest
from unittest.mock import AsyncMock

import httpx

from app.integrations.sub2api.client import Sub2ApiClient


INSTANCE = "9c365a68-bd8d-42be-9b65-8f881b38e91a"
CAPABILITIES = {"instance_id": INSTANCE, "oauth_sync": {"revision": 3, "available": True, "credential_cas": True,
                               "operation_receipts": True, "atomic_receipts": True, "resumable_followups": True, "recovery_modes": ["credentials_only"]}}
RECEIPT = {"contract_version": 1, "operation_id": "fixture-operation", "remote_account_id": 42,
           "credential_write": "succeeded", "token_cache_invalidation": "succeeded",
           "instance_id": INSTANCE, "scheduler_refresh": "succeeded", "state": "completed",
           "auth_recovery": "skipped", "schedulable": True, "remaining_blockers": [], "partial": False}


class CredentialSyncContractTests(unittest.IsolatedAsyncioTestCase):
    async def run_sync(self, responder, **overrides):
        calls = []

        def route(request):
            calls.append(request)
            return responder(request)

        client = Sub2ApiClient()
        http = httpx.AsyncClient(base_url="https://fixture.invalid", transport=httpx.MockTransport(route))
        client._with_client = AsyncMock(return_value=(http, {}, {}))
        arguments = dict(credentials={"access_token": "fixture-at", "refresh_token": "fixture-rt", "client_id": "fixture-client"},
                         expected_identity={"email": "fixture@example.invalid", "workspace_id": None},
                         expected_updated_at="2026-09-08T00:00:00Z", operation_id="fixture-operation")
        arguments.update(overrides)
        result = await client.sync_oauth_credentials(AsyncMock(), 42, **arguments)
        self.assertTrue(http.is_closed or not client._with_client.await_count)
        await http.aclose()
        return result, calls

    @staticmethod
    def response(request, receipt=None):
        return httpx.Response(200, json={"code": 0, "data": CAPABILITIES if request.url.path.endswith("capabilities") else receipt or RECEIPT})

    async def test_sends_client_context_and_version(self):
        result, calls = await self.run_sync(self.response)
        self.assertTrue(result["ok"])
        body = json.loads(calls[1].content)
        self.assertEqual(body["credentials"]["client_id"], "fixture-client")
        self.assertIn("workspace_id", body["expected_identity"])
        self.assertIsNone(body["expected_identity"]["workspace_id"])
        self.assertEqual(body["expected_updated_at"], "2026-09-08T00:00:00Z")
        self.assertNotIn("fixture-at", json.dumps(result))

    async def test_missing_capability_never_writes(self):
        result, calls = await self.run_sync(lambda request: httpx.Response(404))
        self.assertFalse(result["ok"])
        self.assertFalse(result["supported"])
        self.assertEqual([r.method for r in calls], ["GET"])

    async def test_missing_target_never_falls_back(self):
        def respond(request):
            return self.response(request) if request.method == "GET" else httpx.Response(404)
        result, calls = await self.run_sync(respond)
        self.assertEqual(result["error_code"], "remote_account_missing")
        self.assertEqual([r.method for r in calls], ["GET", "POST"])

    async def test_auth_only_is_blocked_before_post(self):
        result, calls = await self.run_sync(self.response, recovery_mode="auth_only")
        self.assertFalse(result["ok"])
        self.assertEqual([r.method for r in calls], ["GET"])

    async def test_partial_and_false_business_success_remain_incomplete(self):
        for changes in ({"partial": True}, {"token_cache_invalidation": "failed"}, {"ok": False}):
            result, _ = await self.run_sync(lambda r: self.response(r, {**RECEIPT, **changes}))
            self.assertFalse(result["ok"])
            self.assertTrue(result["partial"])
            self.assertEqual(result["credential_write"], "succeeded")

    async def test_post_timeout_queries_receipt_without_reposting(self):
        def respond(request):
            if request.method == "POST":
                raise httpx.ReadTimeout("fixture-at must not appear in output", request=request)
            if "credential-sync-operations" in request.url.path:
                return httpx.Response(200, json={"code": 0, "data": {"state": "recorded", "operation_id": "fixture-operation", "receipt": RECEIPT}})
            return self.response(request)
        result, calls = await self.run_sync(respond)
        self.assertTrue(result["ok"])
        self.assertEqual([r.method for r in calls], ["GET", "POST", "GET"])

    async def test_unknown_receipt_does_not_turn_into_failure_or_retry(self):
        def respond(request):
            if request.method == "POST":
                raise httpx.ReadTimeout("fixture secret", request=request)
            if "credential-sync-operations" in request.url.path:
                return httpx.Response(200, json={"data": {"state": "unknown"}})
            return self.response(request)
        result, calls = await self.run_sync(respond)
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(result["credential_write"], "unknown")
        self.assertEqual([r.method for r in calls], ["GET", "POST", "GET"])
        self.assertNotIn("fixture secret", json.dumps(result))

    async def test_wrong_operation_receipt_is_unknown(self):
        result, calls = await self.run_sync(lambda r: self.response(r, {**RECEIPT, "operation_id": "other"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(sum(r.method == "POST" for r in calls), 1)

    async def test_admin_auth_failure_not_account_oauth_failure(self):
        result, calls = await self.run_sync(lambda r: httpx.Response(401, text="fixture-secret"))
        self.assertEqual(result["error_code"], "bridge_admin_auth_failed")
        self.assertNotIn("fixture-secret", json.dumps(result))
        self.assertEqual(len(calls), 1)

    async def test_missing_precondition_never_opens_connection(self):
        result, calls = await self.run_sync(self.response, expected_updated_at=None)
        self.assertEqual(result["error_code"], "sync_precondition_missing")
        self.assertFalse(calls)

    async def test_outer_business_failure_cannot_be_reported_as_success(self):
        def respond(request):
            if request.url.path.endswith("capabilities"):
                return self.response(request)
            return httpx.Response(200, json={"code": 0, "success": False, "data": RECEIPT})
        result, calls = await self.run_sync(respond)
        self.assertFalse(result["ok"])
        self.assertEqual(result["state"], "unknown")
        self.assertEqual(sum(r.method == "POST" for r in calls), 1)

    async def test_unknown_blocker_is_never_complete(self):
        result, _ = await self.run_sync(lambda r: self.response(r, {**RECEIPT, "remaining_blockers": ["future_blocker"]}))
        self.assertFalse(result["ok"])
        self.assertTrue(result["partial"])

    async def test_in_progress_conflict_queries_receipt(self):
        def respond(request):
            if request.method == "POST":
                return httpx.Response(409, json={"reason": "IDEMPOTENCY_IN_PROGRESS"})
            if "credential-sync-operations" in request.url.path:
                return httpx.Response(200, json={"code": 0, "data": {"state": "recorded", "operation_id": "fixture-operation", "receipt": RECEIPT}})
            return self.response(request)
        result, calls = await self.run_sync(respond)
        self.assertTrue(result["ok"])
        self.assertEqual([r.method for r in calls], ["GET", "POST", "GET"])
