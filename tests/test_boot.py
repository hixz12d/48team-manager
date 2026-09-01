import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_client


class BootTests(unittest.TestCase):
    def test_health_and_login_page(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            health = client.get("/health")
            self.assertEqual(health.status_code, 200)
            self.assertEqual(health.json()["app"], "48 Team Manager")

            login = client.get("/login")
            self.assertEqual(login.status_code, 200)
            self.assertIn("48 Team Manager", login.text)
            self.assertNotIn("cartoon", login.text)
            self.assertNotIn("/admin/v2", login.text)

            home = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
            self.assertEqual(home.status_code, 303)
            self.assertEqual(home.headers["location"], "/login")
