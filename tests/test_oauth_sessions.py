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

    def test_install_registers_protocol(self):
        script = oauth_sessions.install_protocol_script()
        self.assertIn(r"HKCU:\Software\Classes\team48-oauth", script)
        self.assertIn("launch.json", script)
        url = oauth_sessions.protocol_url("abc", "https://48team.example")
        self.assertTrue(url.startswith("team48-oauth://launch?"))
        self.assertIn("ticket=abc", url)


if __name__ == "__main__":
    unittest.main()
