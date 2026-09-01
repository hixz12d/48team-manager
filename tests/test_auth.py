import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_client


class AuthTests(unittest.TestCase):
    def test_login_logout_and_console(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            denied = client.get("/api/overview")
            self.assertEqual(denied.status_code, 401)

            bad = client.post("/auth/login", json={"username": "hixz12", "password": "wrong"})
            self.assertEqual(bad.status_code, 401)

            ok = client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            self.assertEqual(ok.status_code, 200)
            self.assertTrue(ok.json()["success"])

            overview = client.get("/", headers={"accept": "text/html"})
            self.assertEqual(overview.status_code, 200)
            self.assertIn("Needs attention", overview.text)
            self.assertIn("All systems healthy", overview.text)
            self.assertNotIn("/admin/v2", overview.text)
            self.assertNotIn("/admin/v3", overview.text)

            payload = client.get("/api/overview")
            self.assertEqual(payload.status_code, 200)
            self.assertTrue(payload.json()["healthy"])
            self.assertEqual(payload.json()["attention"], [])

            settings = client.get("/api/settings")
            self.assertEqual(settings.status_code, 200)
            self.assertEqual(settings.json()["secrets"]["sub2api_api_key"], "••••••")
            self.assertFalse(settings.json()["automation"]["auto_rotate"])

            client.post("/auth/logout")
            self.assertEqual(client.get("/api/overview").status_code, 401)
