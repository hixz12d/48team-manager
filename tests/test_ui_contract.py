import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_client


FORBIDDEN_UI = (
    "style.css",
    "main.js",
    "admin_v2",
    "admin/v2",
    "admin/v3",
    "cartoon",
    "warm theme",
    "redemption",
    "warranty",
    "welfare",
)

PAGES = (
    "/",
    "/workspaces",
    "/accounts",
    "/operations",
    "/resources/phones",
    "/resources/hme",
    "/resources/proxies",
    "/settings",
)


class UIContractTests(unittest.TestCase):
    def test_console_has_one_product_and_no_legacy_assets(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            js = client.get("/static/js/app.js").text
            self.assertIn("AbortController", js)
            self.assertIn("abortEntity", js)
            self.assertIn("controllers", js)
            self.assertNotIn("tbody.innerHTML", js)

            for path in PAGES:
                response = client.get(path, headers={"accept": "text/html"})
                self.assertEqual(response.status_code, 200, path)
                body = response.text
                self.assertIn("48 Team Manager", body)
                self.assertIn("/static/css/tokens.css", body)
                self.assertIn("/static/js/app.js", body)
                for needle in FORBIDDEN_UI:
                    self.assertNotIn(needle, body, f"{path} still mentions {needle}")
                self.assertIn('id="operations-drawer"', body)
                self.assertIn("hidden", body)

            accounts = client.get("/accounts").text
            self.assertIn('value="archived"', accounts)
            self.assertNotIn(">Archive<", accounts)
            self.assertNotIn("danger-archive", accounts)

    def test_settings_never_returns_raw_secret(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            payload = client.get("/api/settings").json()
            self.assertTrue(all(value == "••••••" for value in payload["secrets"].values()))
