import unittest

from app.services.oauth_sessions import chrome_proxy_parts, create_session, get_session, launcher_script
from app.services.reauth import auto_reauth_plan, is_icloud_email, is_oauth_callback, owner_refresh_allows_oauth


class ReauthPlanTests(unittest.TestCase):
    def test_owner_is_manual(self):
        plan = auto_reauth_plan(email="mom@gmail.com", role="owner", password="x", proxy="socks5h://u:p@1.2.3.4:1080")
        self.assertFalse(plan["auto"])

    def test_owner_refresh_only_falls_back_when_tokens_are_dead(self):
        self.assertTrue(owner_refresh_allows_oauth("token_refresh_failed"))
        self.assertTrue(owner_refresh_allows_oauth(""))
        self.assertFalse(owner_refresh_allows_oauth("team_banned"))
        self.assertFalse(owner_refresh_allows_oauth("token_identity_mismatch"))

    def test_icloud_with_password_mail_and_proxy_is_auto(self):
        self.assertTrue(is_icloud_email("kid@icloud.com"))
        plan = auto_reauth_plan(
            email="kid@icloud.com",
            role="child",
            password="secret",
            cf_ready=True,
            proxy="socks5h://u:p@1.2.3.4:1080",
        )
        self.assertTrue(plan["auto"])

    def test_icloud_without_proxy_falls_back(self):
        plan = auto_reauth_plan(email="kid@icloud.com", role="child", password="secret", cf_ready=True, proxy="")
        self.assertFalse(plan["auto"])

    def test_callback_detection(self):
        self.assertTrue(is_oauth_callback("http://localhost:1455/auth/callback?code=abc&state=1"))
        self.assertFalse(is_oauth_callback("https://auth.openai.com/oauth/authorize"))


class ProxiedLauncherTests(unittest.TestCase):
    def test_chrome_proxy_parts_mask_password(self):
        parts = chrome_proxy_parts("socks5h://user:secret@10.0.0.8:1080")
        self.assertEqual(parts["server"], "socks5://10.0.0.8:1080")
        self.assertEqual(parts["username"], "user")
        self.assertEqual(parts["password"], "secret")
        self.assertIn("10.0.0.8:1080", parts["label"])
        self.assertNotIn("secret", parts["label"])

    def test_launcher_uses_proxy_and_bypasses_localhost(self):
        session = create_session(
            team_id=1,
            email="mom@gmail.com",
            authorize={"authorize_url": "https://auth.openai.com/oauth/authorize?x=1", "code_verifier": "hidden"},
            role="owner",
            proxy="http://user:secret@10.0.0.8:8000",
        )
        self.assertIn("10.0.0.8:8000", session["proxy_label"])
        self.assertNotIn("secret", session["proxy_label"])
        self.assertNotIn("code_verifier", session)
        stored = get_session(session["ticket"])
        script = launcher_script(stored, "https://48team.example/admin/seats/oauth/complete")
        self.assertIn('"proxyServer":"http://10.0.0.8:8000"', script)
        self.assertIn("--proxy-server=' + $cfg.proxyServer", script)
        self.assertIn("proxy-bypass-list=localhost;127.0.0.1;<-loopback>", script)
        self.assertIn("secret", script)
        self.assertNotIn("hidden", script)


if __name__ == "__main__":
    unittest.main()
