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


if __name__ == "__main__":
    unittest.main()
