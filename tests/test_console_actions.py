import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from tests.helpers import make_client


class ConsoleActionsContractTests(unittest.TestCase):
    def test_settings_bootstrap_and_probe_target(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            js = client.get("/static/js/app.js").text
            self.assertIn("pageBootstraps", js)
            self.assertIn("bootSettings", js)
            self.assertEqual(js.count('else if (page === "settings")'), 0)
            self.assertIn("entityActions", js)
            self.assertIn("toast(", js)
            self.assertIn('id="toast-region"', client.get("/settings").text)

            with (
                patch("app.web.routes.api.probe_sub2api", new=AsyncMock(return_value={"ok": True, "checked": "sub2api"})),
                patch("app.web.routes.api.probe_hme", new=AsyncMock(return_value={"ok": True, "checked": "hme"})) as hme,
                patch("app.web.routes.api.probe_mail", new=AsyncMock(return_value={"ok": True, "checked": "mail"})) as mail,
            ):
                response = client.post("/api/settings/probe", json={"target": "sub2api", "connections": {}})
            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertEqual(payload["target"], "sub2api")
            self.assertTrue(payload["sub2api"]["ok"])
            self.assertTrue(payload["hme"].get("skipped"))
            self.assertTrue(payload["mail"].get("skipped"))
            hme.assert_not_called()
            mail.assert_not_called()

    def test_visible_actions_exist_in_registry(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            js = client.get("/static/js/app.js").text
            for action in (
                "workspace.sync",
                "workspace.manage",
                "team.member.invite",
                "team.member.link",
                "account.refresh",
                "account.quota",
                "account.sub2api",
                "hme.retry-label",
                "operation.cancel",
                "operation.retry",
            ):
                self.assertIn(f'id: "{action}"', js)
            phones = client.get("/resources/phones").text
            proxies = client.get("/resources/proxies").text
            workspaces = client.get("/workspaces").text
            hme = client.get("/resources/hme").text
            self.assertIn("data-open-phone-import", phones)
            self.assertIn("只读", proxies)
            self.assertIn("数据源 Sub2API", proxies)
            self.assertIn("data-action-page=\"workspace-sync-all\"", workspaces)
            self.assertIn("data-action-page=\"hme-reconcile\"", hme)
            self.assertIn("data-open-register", client.get("/accounts").text)

    def test_phone_import_and_proxy_write_boundary(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            imported = client.post(
                "/api/resources/phones/import",
                json={"text": "+15550001111----https://sms.example/a\n+15550001111----https://sms.example/a"},
            )
            self.assertEqual(imported.status_code, 200)
            body = imported.json()
            self.assertTrue(body["ok"])
            self.assertGreaterEqual(body.get("imported", 0), 1)
            created = client.post(
                "/api/resources/proxies",
                json={"url": "socks5h://user:pass@127.0.0.1:1080", "name": "lab"},
            )
            self.assertEqual(created.status_code, 405)
