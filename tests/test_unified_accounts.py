import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.helpers import make_client


class UnifiedAccountApiTests(unittest.TestCase):
    def test_registration_queue_and_local_reads(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            self.assertEqual(client.post('/api/accounts', json={'email':'new@example.com'}).status_code, 401)
            client.post('/auth/login', json={'username':'hixz12', 'password':'test-password'})
            bad = client.post('/api/accounts', json={'email':'bad address'})
            self.assertEqual(bad.status_code, 422)
            extra = client.post('/api/accounts', json={'email':'new@example.com', 'access_token':'not-accepted'})
            self.assertEqual(extra.status_code, 422)
            with patch('app.application.quota.quota_service.client.fetch_quota', new=AsyncMock()) as upstream:
                created = client.post('/api/accounts', json={'email':'New@example.com', 'purpose':'standby'})
                self.assertEqual(created.status_code, 201)
                account = created.json()['account']
                self.assertEqual(account['email'], 'new@example.com')
                self.assertEqual(account['health']['code'], 'unauthorized')
                duplicate = client.post('/api/accounts', json={'email':'new@example.com', 'purpose':'child'})
                self.assertEqual(duplicate.status_code, 409)
                portfolio = client.get('/api/accounts/portfolio').json()
                self.assertEqual(portfolio['accounts'][0]['purpose'], 'standby')
                self.assertEqual(portfolio['summary']['needs_auth'], 1)
                first = client.post(f"/api/accounts/{account['id']}/quota/probe")
                second = client.post(f"/api/accounts/{account['id']}/quota/probe")
                self.assertEqual(first.status_code, 202)
                self.assertEqual(first.json()['operation_id'], second.json()['operation_id'])
                operation = client.get('/api/operations/' + first.json()['operation_id']).json()
                self.assertEqual(operation['state'], 'queued')
                client.get('/api/accounts')
                runtime = client.get('/api/quota/runtime').json()
                self.assertEqual(runtime['queued_count'], 1)
                self.assertFalse(runtime['effective_enabled'])
                upstream.assert_not_called()
