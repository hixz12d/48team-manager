import unittest

from app.services import oauth_sessions


class OAuthSessionTests(unittest.TestCase):
    def test_public_session_hides_verifier(self):
        session = oauth_sessions.create_session(
            team_id=9,
            email="scheme-nougats-0p@icloud.com",
            authorize={
                "authorize_url": "https://auth.openai.com/oauth/authorize?x=1",
                "code_verifier": "secret-verifier",
                "state": "abc",
                "client_id": oauth_sessions.CLIENT_ID,
            },
        )
        self.assertEqual(session["email"], "scheme-nougats-0p@icloud.com")
        self.assertNotIn("code_verifier", session)
        stored = oauth_sessions.get_session(session["ticket"])
        self.assertEqual(stored["code_verifier"], "secret-verifier")
        script = oauth_sessions.launcher_script(stored, "https://48team.example/admin/seats/oauth/complete")
        self.assertIn("127.0.0.1:1455", script)
        self.assertIn(session["ticket"], script)
        self.assertNotIn("secret-verifier", script)

    def test_protocol_handler_fetches_launch_json(self):
        script = oauth_sessions.protocol_handler_script()
        self.assertIn("launch.json", script)
        self.assertIn("/ack", script)
        self.assertIn("127.0.0.1:1455", script)
        self.assertIn("Tls12", script)
        self.assertIn("Team48SocksBridge", script)
        self.assertIn("FromBase64String", script)
        self.assertIn("http://localhost:1455/", script)
        self.assertIn("Stop-Team48Oauth", script)
        self.assertIn("ShowWindow", script)
        self.assertIn("loginEmail", script)
        self.assertIn("fill.js", script)
        self.assertIn("disable-background-networking", script)
        self.assertIn("teamName", script)
        self.assertIn("IsLoopback", oauth_sessions.socks_bridge_source())
        self.assertIn("Warm", oauth_sessions.socks_bridge_source())
        self.assertIn("google-analytics", oauth_sessions.oauth_bg_source())
        self.assertNotIn("'@", script)

    def test_install_registers_protocol(self):
        script = oauth_sessions.install_protocol_script()
        self.assertIn(r"HKCU:\Software\Classes\team48-oauth", script)
        self.assertIn("launch.json", script)
        self.assertIn("UTF8Encoding $true", script)
        self.assertIn("Team48SocksBridge", script)
        self.assertNotIn("'@\n", oauth_sessions.protocol_handler_script())
        url = oauth_sessions.protocol_url("abc", "https://48team.example")
        self.assertTrue(url.startswith("team48-oauth://launch?"))
        self.assertIn("ticket=abc", url)

    def test_launch_payload_keeps_password_off_public_session(self):
        session = oauth_sessions.create_session(
            team_id=9,
            email="kid@icloud.com",
            authorize={
                "authorize_url": "https://auth.openai.com/oauth/authorize?x=1",
                "code_verifier": "secret-verifier",
                "state": "abc",
                "client_id": oauth_sessions.CLIENT_ID,
            },
            password="child-pass-1",
        )
        self.assertNotIn("child-pass-1", str(session))
        stored = oauth_sessions.get_session(session["ticket"])
        payload = oauth_sessions.launch_payload(stored, "https://48team.example/admin/seats/oauth/complete")
        self.assertEqual(payload["loginEmail"], "kid@icloud.com")
        self.assertEqual(payload["loginPassword"], "child-pass-1")
        script = oauth_sessions.launcher_script(stored, "https://48team.example/admin/seats/oauth/complete")
        self.assertIn("child-pass-1", script)
        self.assertIn("window.TEAM48_EMAIL", script)
        self.assertNotIn("'@", oauth_sessions.oauth_fill_source())


if __name__ == "__main__":
    unittest.main()
