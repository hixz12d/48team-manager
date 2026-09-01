import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.helpers import make_client


class ConnectionProbeTests(unittest.TestCase):
    def test_probe_uses_form_values_without_saving_secrets(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            groups = [{"id": 12, "name": "Team A", "account_count": 2}]
            accounts = [
                {"id": 1, "email": "owner@example.com", "group_ids": [12], "name": "Pedro-owner"},
                {"id": 2, "email": "child@example.com", "group_ids": [12], "name": "Pedro-child-1"},
            ]
            aliases = [
                {"email": "a@icloud.com", "active": True, "label": "12"},
                {"email": "b@icloud.com", "active": True, "label": "GPT已使用"},
                {"email": "c@icloud.com", "active": False, "label": "3"},
            ]
            with (
                patch("app.application.connection_probe.sub2api_client.list_groups", new=AsyncMock(return_value=groups)),
                patch(
                    "app.application.connection_probe.sub2api_client.list_status_accounts",
                    new=AsyncMock(return_value=accounts),
                ),
                patch(
                    "app.application.connection_probe.hme_client.list_accounts",
                    return_value=[{"id": "acc-1", "name": "iCloud", "status": "active"}],
                ),
                patch("app.application.connection_probe.hme_client.list_aliases", return_value=aliases),
                patch(
                    "app.integrations.mail.cloudflare.cloudflare_mail_client.fetch_messages",
                    return_value=[{"subject": "hi"}],
                ),
            ):
                response = client.post(
                    "/api/settings/probe",
                    json={
                        "connections": {
                            "sub2api_base_url": "http://sub2api-canary:8080",
                            "sub2api_api_key": "live-key",
                            "hme_base_url": "http://icloud-hme:8081",
                            "hme_service_token": "live-hme",
                            "hme_account_id": "acc-1",
                            "cf_mail_base_url": "https://mail.example.com",
                            "cf_mail_address": "box@example.com",
                            "cf_mail_admin_password": "mail-secret",
                        }
                    },
                )
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["sub2api"]["group_count"], 1)
            self.assertEqual(payload["sub2api"]["groups"][0]["name"], "Team A")
            self.assertIn("owner@example.com", payload["sub2api"]["groups"][0]["owners"])
            self.assertNotIn("child@example.com", payload["sub2api"]["groups"][0]["owners"])
            self.assertEqual(payload["hme"]["alias_count"], 3)
            self.assertEqual(payload["hme"]["unused_count"], 1)
            self.assertEqual(payload["mail"]["address"], "box@example.com")

            saved = client.get("/api/settings").json()
            self.assertEqual(saved["secrets"]["sub2api_api_key"], "••••••")
            self.assertFalse(saved["connections"]["hme"]["configured"])
