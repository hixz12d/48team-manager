import tempfile
import unittest
from pathlib import Path

from tests.helpers import make_client
from pydantic import ValidationError

from app.web.schemas.resources import AccountAutomationPatch, AccountProxyPatch, OnboardRequest, ReplenishRequest


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
    def test_proxy_and_automation_payload_contracts(self):
        self.assertTrue(AccountAutomationPatch(auto_reauth_opt_in=True).auto_reauth_opt_in)
        selected = AccountProxyPatch(proxy_selection={"source": "sub2api", "remote_id": 7})
        self.assertEqual(selected.proxy_selection.remote_id, 7)
        onboard = OnboardRequest(proxy_selection={"source": "sub2api", "remote_id": 9})
        self.assertEqual(onboard.proxy_selection.remote_id, 9)
        with self.assertRaises(ValidationError):
            AccountProxyPatch(proxy="socks5://127.0.0.1:1080", clear=True)
        with self.assertRaises(ValidationError):
            OnboardRequest(
                proxy="socks5://127.0.0.1:1080",
                proxy_selection={"source": "sub2api", "remote_id": 7},
            )
        self.assertEqual(
            ReplenishRequest(phone_line="+17822063428----https://api668.com/sms/by_key?key=test").phone_line,
            "+17822063428----https://api668.com/sms/by_key?key=test",
        )
        self.assertEqual(ReplenishRequest().phone_line, "")
        with self.assertRaises(ValidationError):
            ReplenishRequest(phone_line="+17822063428")
    def test_console_has_one_product_and_no_legacy_assets(self):
        with tempfile.TemporaryDirectory() as tmp, make_client(Path(tmp)) as client:
            client.post("/auth/login", json={"username": "hixz12", "password": "test-password"})
            js = client.get("/static/js/app.js").text
            self.assertIn("AbortController", js)
            self.assertIn("abortEntity", js)
            self.assertIn("readControllers", js)
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
                self.assertNotIn('id="operations-drawer"', body)
                self.assertIn('href="/operations" class="nav-link', body)
                self.assertNotIn("data-open-operations", body)
                self.assertIn("hidden", body)

            accounts = client.get("/accounts").text
            self.assertIn('value="archived"', accounts)
            self.assertNotIn(">Archive<", accounts)
            self.assertNotIn("danger-archive", accounts)
            self.assertIn('id="register-form"', accounts)  # shared overlay still present
            self.assertIn("data-open-register", accounts)
            self.assertIn('id="reauth-sheet"', accounts)
            self.assertIn('id="confirm-sheet"', accounts)
            self.assertIn('id="account-selection-bar"', accounts)
            self.assertIn("reauth-authorize-url", accounts)
            workspaces = client.get("/workspaces").text
            self.assertIn('id="register-form"', workspaces)
            self.assertIn("登记团队", workspaces)

            self.assertIn("生成授权链接", workspaces)
            self.assertIn("callback_url", workspaces)
            self.assertNotIn("official_workspace_id", workspaces)
            self.assertNotIn("Access Token", workspaces)
            self.assertIn('name="proxy_remote_id"', workspaces)
            self.assertNotIn('name="proxy"', workspaces)
            self.assertIn("proxy_selection", js)
            self.assertNotIn("socks5://host:port", js)

            phones = client.get("/resources/phones").text
            self.assertIn('data-open-phone-import', phones)
            self.assertIn('id="phone-import-form"', phones)
            self.assertIn("号码----", phones)
            proxies = client.get("/resources/proxies").text
            self.assertIn("只读", proxies)
            self.assertIn("数据源 Sub2API", proxies)
            self.assertNotIn('data-open-proxy-add', proxies)
            self.assertNotIn('id="proxy-add-form"', proxies)
            self.assertNotIn('data-proxy-edit-profile', proxies)
            self.assertNotIn('id="proxy-add-sheet"', proxies)
            overview = client.get("/").text
            self.assertIn("overview-layout", overview)
            self.assertIn("overview-health", overview)
            accounts = client.get("/accounts").text
            self.assertIn('data-management-view="teams"', accounts)
            self.assertIn('id="accounts-portfolio"', accounts)
            js = client.get("/static/js/app.js").text
            self.assertIn('"account-owner": "所有者"', js)
            self.assertNotIn('"account-owner": "成员"', js)
            self.assertIn("quota-meter", js)
            self.assertIn("/api/accounts/portfolio", client.get("/static/js/accounts-view.js").text)
            self.assertNotIn("/api/workspaces/${item.id}/sync-name", js)
            self.assertNotIn("同步官方名称", js)
            self.assertIn("is-collapsed", js)
            self.assertNotIn("fillProxyProfileOptions", js)
            self.assertIn("quota-billing-summary", js)
            self.assertIn("item.exit_ip", js)
            self.assertIn("item.checked_at", js)
            self.assertNotIn("/api/sub2api/templates", js)
            self.assertIn("meter-window", js)
            self.assertIn("toggle-icon", js)
            self.assertIn("接入", js)
            self.assertNotIn("纳管", js)
            self.assertIn("母号", js)
            self.assertNotIn("母号 (Admin)", js)
            self.assertNotIn("绑定已有档案", proxies)
            self.assertIn('id="entity-sheet"', workspaces)
            self.assertIn('id: "workspace.manage"', js)
            self.assertIn('id: "team.replenish"', js)
            self.assertIn('id: "team.member.invite"', js)
            self.assertIn('id: "team.member.link"', js)
            self.assertIn("function openWorkspaceDetails", js)
            self.assertIn("function showTeamAuthStep", js)
            self.assertIn("team-member-list", js)
            self.assertIn("邀请加入 Team", js)
            self.assertIn("补充 Team", js)
            self.assertIn("/api/workspaces/${workspace.id}/replenish", js)
            self.assertIn("renderTeamReplenishControls", js)
            self.assertIn("官方席位固定邀请 Premium", js)
            self.assertNotIn("askReplenishPhoneLine", js)
            self.assertIn("改成 Owner", js)
            self.assertIn("改成 Member", js)
            self.assertIn("永久删除", js)
            self.assertIn("/api/workspaces/${workspace.id}/members/remove", js)
            self.assertIn('kind === "invited" ? "revoke-invite" : "kick"', js)
            self.assertIn("/api/workspaces/${workspace.id}/members/purge", js)
            self.assertIn("/api/workspaces/${workspace.id}/members/role", js)
            self.assertIn("aria-busy", js)
            self.assertIn('tone === "error" ? "alert" : "status"', js)
            self.assertIn('label: "技术详情"', js)
            self.assertNotIn("manage-children-sheet", accounts)
            self.assertNotIn("manage-child-remove-sheet", accounts)
            self.assertNotIn("onboard-sheet", accounts)
            self.assertNotIn("rotate-sheet", accounts)
            self.assertNotIn("workspace.manage-children", js)
            self.assertNotIn("管理子号", js)
            self.assertNotIn("创建子号", accounts)
            self.assertNotIn("更换子号", accounts)
            self.assertNotIn("查看任务", js)
            self.assertNotIn("去任务", js)
            self.assertNotIn("任务 ID", js)
            self.assertNotIn('<th scope="col" class="actions">操作</th>', accounts)
            self.assertNotIn('<th scope="col" class="actions">操作</th>', workspaces)
            self.assertIn("row-actions-contextual", js)
            self.assertNotIn("overlayReturn", js)
            self.assertNotIn("activeModal", js)
            self.assertNotIn("openModalOverlay", js)
            self.assertNotIn("closeModalOverlay", js)
            self.assertIn("overlayState", js)
            self.assertIn("function openOverlay", js)
            self.assertIn("function openConfirm", js)
            self.assertNotIn("window.prompt", js)
            self.assertIn("/api/accounts/delete-local", client.get("/static/js/accounts-view.js").text)
            self.assertIn("function replaceOverlay", js)
            self.assertIn("function closeOverlay", js)
            self.assertIn("function restoreOverlayContext", js)
            self.assertIn("function getActiveOverlay", js)
            self.assertIn("function openRegister", js)
            self.assertIn("function closeRegister", js)
            self.assertIn("function openReauth", js)
            self.assertIn("function closeReauth", js)
            self.assertIn("async function submitReauth", js)
            self.assertIn("function workspacePrimaryAction", js)
            self.assertIn("function presentTeamMember", js)
            self.assertIn("function teamMemberKind", js)
            self.assertIn('if (remoteState === "joined" || status === "managed" || status === "remote_only")', js)
            self.assertNotIn('if (kind === "invited" || row.membership_state === "invited")', js)
            self.assertIn("owner_needs_auth", js)
            self.assertIn("owner_auth_action", js)
            self.assertIn("owner_account_id", js)
            self.assertIn("startCurrentOperation", js)
            self.assertIn("account-auto-reauth-button", js)
            self.assertIn('button.textContent = active ? autoReauthProgressLabel(entry.state) : "开始自动授权"', js)
            self.assertIn('label: "查看进度"', js)
            self.assertNotIn("确认将 ${item.email} 加入自动重授权队列", js)
            self.assertIn("inFlightWrites", js)
            self.assertIn("`account:${account.id}:reauth:complete:${ticket}`", js)
            self.assertIn("`workspace:${workspace.id}:member:${email}:link`", js)
            self.assertIn("`/api/operations?${query.toString()}`", js)
            self.assertNotIn('fetchEntity("operation-list"', js.split("async function bootOperations")[0])
            components_css = client.get("/static/css/components.css").text
            self.assertIn(".team-member-row.is-selected", components_css)
            self.assertIn(".palette", components_css)
            self.assertIn(".confirm-sheet", components_css)
            self.assertIn(".confirm-panel", components_css)
            self.assertIn(".palette::backdrop", components_css)
            self.assertIn('id="command-palette"', workspaces)

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
            self.assertIn('name="auto_reauth"', settings_form)
            self.assertTrue(
                {"requested", "deployment_allowed", "effective"}.issubset(
                    payload["automation"]["auto_reauth"]
                )
            )
            self.assertNotIn('name="auto_rotate"', settings_form)
            self.assertNotIn('name="force_refill"', settings_form)
            self.assertNotIn('id="rotate-sheet"', page)
            self.assertIn("优先查看异常", client.get("/").text)
