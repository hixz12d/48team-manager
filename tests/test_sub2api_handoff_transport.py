import json
import unittest
from unittest.mock import AsyncMock, patch
import httpx
import tests.test_sub2api_sync_contract as contract
from app.integrations.sub2api.refresh_handoff import handoff_call
from app.integrations.sub2api.client import sub2api_client


class RefreshHandoffTransportTests(unittest.IsolatedAsyncioTestCase):
    run_sync = contract.CredentialSyncContractTests.run_sync

    async def test_returned_binding_never_sends_rt_back_in_sync(self):
        with patch("app.application.refresh_ownership.remote_accepts_access_token_only", new=AsyncMock(return_value=True)):
            result, requests = await self.run_sync(contract.CredentialSyncContractTests.response)
        self.assertTrue(result["ok"])
        sent = json.loads(requests[1].content)["credentials"]
        self.assertIn("access_token", sent)
        self.assertNotIn("refresh_token", sent)
        self.assertNotIn("id_token", sent)

    async def test_old_server_is_blocked_before_mutation(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"code": 0, "data": contract.CAPABILITIES})
        http = httpx.AsyncClient(base_url="https://fixture.invalid", transport=httpx.MockTransport(respond))
        with patch.object(sub2api_client, "_with_client", new=AsyncMock(return_value=(http, {}, {}))):
            result = await handoff_call(AsyncMock(), 42, {"expected_instance_id": contract.INSTANCE, "operation_id": "fixture-handoff"}, "prepare")
        self.assertEqual(result["error_code"], "handoff_unsupported")
        self.assertEqual([r.method for r in requests], ["GET"])

    async def test_wrong_receipt_or_auth_failure_does_not_expose_secret(self):
        for status in (200, 403):
            def respond(request):
                if request.method == "GET":
                    return httpx.Response(200, json={"code": 0, "data": {"instance_id": contract.INSTANCE, "oauth_sync": {"revision": 5, "refresh_handoff": True, "refresh_fencing": True, "refresh_fencing_scope": "observed_rt_lineage"}}})
                return httpx.Response(status, json={"code": 0, "data": {"operation_id": "wrong-operation", "credentials": {"refresh_token": "fixture-secret-RT"}}})
            http = httpx.AsyncClient(base_url="https://fixture.invalid", transport=httpx.MockTransport(respond))
            with patch.object(sub2api_client, "_with_client", new=AsyncMock(return_value=(http, {}, {}))):
                result = await handoff_call(AsyncMock(), 42, {"expected_instance_id": contract.INSTANCE, "operation_id": "fixture-handoff"}, "read")
            self.assertFalse(result["ok"])
            self.assertNotIn("fixture-secret-RT", json.dumps(result))
