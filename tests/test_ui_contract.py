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
            self.assertIn('id="register-form"', accounts)  # shared overlay still present
            self.assertNotIn("data-open-register", accounts)
            self.assertIn('id="reauth-sheet"', accounts)
            self.assertIn("reauth-authorize-url", accounts)
            workspaces = client.get("/workspaces").text
            self.assertIn('id="register-form"', workspaces)
            self.assertIn("登记团队", workspaces)

            self.assertIn("生成授权链接", workspaces)
            self.assertIn("callback_url", workspaces)
            self.assertNotIn("official_workspace_id", workspaces)
            self.assertNotIn("Access Token", workspaces)

            phones = client.get("/resources/phones").text
            self.assertIn('data-open-phone-import', phones)
            self.assertIn('id="phone-import-form"', phones)
            self.assertIn("号码----", phones)
            proxies = client.get("/resources/proxies").text
            self.assertIn('data-open-proxy-add', proxies)
            self.assertIn('id="proxy-add-form"', proxies)
            self.assertIn('data-proxy-edit-profile', proxies)
            overview = client.get("/").text
            self.assertIn("overview-layout", overview)
            self.assertIn("overview-health", overview)
            accounts = client.get("/accounts").text
            self.assertIn('data-accounts-view="portfolio"', accounts)
            self.assertIn('id="accounts-portfolio"', accounts)
            js = client.get("/static/js/app.js").text
            self.assertIn('"account-owner": "所有者"', js)
            self.assertNotIn('"account-owner": "成员"', js)
            self.assertIn("quota-meter", js)
            self.assertIn("/api/accounts/portfolio", js)
            self.assertNotIn("/api/workspaces/${item.id}/sync-name", js)
            self.assertNotIn("同步官方名称", js)
            self.assertIn("is-collapsed", js)
            self.assertIn("fillProxyProfileOptions", js)
            self.assertIn("meter-window", js)
            self.assertIn("toggle-icon", js)
            self.assertIn("接入", js)
            self.assertNotIn("纳管", js)
            self.assertIn("母号", js)
            self.assertNotIn("母号 (Admin)", js)
            self.assertIn("绑定已有档案", proxies)
            self.assertIn("manage-children-sheet", accounts)
            self.assertIn("workspace.manage-children", js)
            self.assertIn("管理子号", js)
            self.assertIn("manage-children-list", accounts)
            self.assertIn("邀请进 Team", accounts)
            self.assertIn("会向官方 Team 发邀请", accounts)
            self.assertIn("所有者 Owner（默认）", accounts)
            self.assertIn('name="role"', accounts)
            self.assertNotIn("只写本地档案，不邀请官方席位", accounts)
            self.assertIn("正在发送官方邀请", js)
            self.assertIn("已邀请进官方席位", js)
            self.assertIn("function needsAuth", js)
            self.assertIn('label: "授权"', js)
            self.assertNotIn("成员漂移", js)
            self.assertIn("本地和官方对不上", js)
            self.assertIn("这个号还没授权", js)
            self.assertIn("点「授权」", accounts)
            self.assertIn("/api/workspaces/${workspaceId}/members/remove", js)
            self.assertIn("/api/workspaces/${workspaceId}/kick", js)
            self.assertIn("/api/workspaces/${workspaceId}/members/purge", js)
            self.assertIn("manage-child-remove-sheet", accounts)
            self.assertIn("踢出官方席位", accounts)
            self.assertIn("只从本地拿掉", accounts)
            self.assertIn("永久删除", accounts)
            self.assertIn('value="purge"', accounts)
            self.assertIn('textContent = kind === "unmanaged" || kind === "invited" ? "踢出" : "删除"', js)
            self.assertNotIn("删除子号稍后补上", js)
            self.assertNotIn("删除子号会在下一步加上", accounts)

    def test_settings_never_returns_raw_secret(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            payload = client.get("/api/settings").json()
            self.assertTrue(all(value == "" for value in payload["secrets"].values()))
            self.assertEqual(payload["secret_state"]["sub2api_api_key"], "missing")
            page = client.get("/settings").text
            self.assertIn("settings-columns", page)
            self.assertIn('id="password-form"', page)
            self.assertIn("sms_cooldown_min", page)
            self.assertIn('name="official_quota_probe"', page)
            settings_form = page.split('id="settings-form"', 1)[1].split('id="password-form"', 1)[0]
            self.assertNotIn('name="auto_reauth"', settings_form)
            self.assertNotIn('name="auto_rotate"', settings_form)
            self.assertNotIn('name="force_refill"', settings_form)
            # Shared rotate sheet may include force_refill outside settings form.
            self.assertIn('id="rotate-sheet"', page)
            self.assertIn("优先查看异常", client.get("/").text)
