import json
import unittest

import httpx
from app.integrations.sub2api.client import Sub2ApiClient
from app.integrations.sub2api.sync_state import request_access_token
from tests.test_sub2api_remote_state import INSTANCE, STAMP


class AccessReadbackTransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_conditional_post_filters_secret_response(self):
        seen = []
        def respond(request):
            seen.append(request)
            return httpx.Response(200, json={"code": 0, "data": {
                "schema_version": 1, "instance_id": INSTANCE, "remote_account_id": 42,
                "credential_version": 7, "account_updated_at": STAMP, "refresh_configured": True,
                "access_token": "fixture-AT", "client_id": "fixture-client", "refresh_token": "must-be-dropped"}})
        client = Sub2ApiClient()
        async def opened(_):
            return httpx.AsyncClient(base_url="https://fixture.invalid", transport=httpx.MockTransport(respond)), {"x-api-key": "fixture-admin"}, {}
        client._with_client = opened
        result = await request_access_token(client, None, 42, INSTANCE, 7, STAMP)
        self.assertTrue(result["ok"])
        self.assertEqual(result["access_token"], "fixture-AT")
        self.assertNotIn("refresh_token", result)
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].method, "POST")
        self.assertTrue(seen[0].url.path.endswith("/credential-sync-access-token"))
        self.assertEqual(json.loads(seen[0].content), {"expected_instance_id": INSTANCE, "expected_credential_version": 7, "expected_updated_at": STAMP})

    async def test_rejected_and_malformed_responses_never_use_general_account_get(self):
        client = Sub2ApiClient()
        for status in (403, 404, 409, 500, 200):
            seen = []
            def respond(request):
                seen.append(request)
                return httpx.Response(status, json={"access_token": "fixture-secret", "message": "fixture-secret"})
            async def opened(_):
                return httpx.AsyncClient(base_url="https://fixture.invalid", transport=httpx.MockTransport(respond)), {}, {}
            client._with_client = opened
            result = await request_access_token(client, None, 42, INSTANCE, 7, STAMP)
            self.assertFalse(result["ok"])
            self.assertNotIn("fixture-secret", json.dumps(result))
            self.assertEqual(len(seen), 1)
