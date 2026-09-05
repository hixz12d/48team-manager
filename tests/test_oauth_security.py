import unittest

from app.application.oauth_sessions import OAuthSessionError, _state_hash, parse_strict_callback


class StrictOAuthCallbackTests(unittest.TestCase):
    redirect_uri = "http://localhost:1455/auth/callback"
    state = "expected-state"

    def parse(self, callback: str):
        return parse_strict_callback(
            callback,
            redirect_uri=self.redirect_uri,
            expected_state_hash=_state_hash(self.state),
        )

    def test_accepts_exact_callback(self):
        parsed = self.parse(
            "http://localhost:1455/auth/callback?code=one-time-code&state=expected-state"
        )
        self.assertEqual(parsed["code"], "one-time-code")

    def test_rejects_missing_or_wrong_state(self):
        for callback in (
            "http://localhost:1455/auth/callback?code=code",
            "http://localhost:1455/auth/callback?code=code&state=wrong",
        ):
            with self.subTest(callback=callback), self.assertRaises(OAuthSessionError):
                self.parse(callback)

    def test_rejects_lookalike_and_duplicate_parameters(self):
        callbacks = (
            "https://evil.example/callback?next=http://localhost:1455/auth/callback&code=x&state=expected-state",
            "http://localhost:1455/auth/callback?code=x&code=y&state=expected-state",
            "http://localhost:1455/auth/callback?code=x&state=expected-state&state=other",
            "http://localhost:1455/other?code=x&state=expected-state",
        )
        for callback in callbacks:
            with self.subTest(callback=callback), self.assertRaises(OAuthSessionError):
                self.parse(callback)

    def test_rejects_bare_code(self):
        with self.assertRaises(OAuthSessionError):
            self.parse("bare-code")
