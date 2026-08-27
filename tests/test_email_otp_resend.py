import unittest
from unittest.mock import MagicMock, patch

from app.services.browser_onboard import (
    OTP_RESEND_MAX,
    OTP_WAIT_SEC,
    _resend_email_otp,
    _wait_mailbox_code,
    wait_email_otp_with_resend,
)


MAIL = dict(
    email="kid@icloud.com",
    pickup_url="",
    proxy="socks5h://user:pass@127.0.0.1:1080",
    use_cloudflare=True,
    cf_base_url="https://apimail.example",
    cf_address="icloud@example.test",
    cf_admin_password="secret",
)


class EmailOtpResendTests(unittest.TestCase):
    def test_wait_uses_sixty_seconds(self):
        captured = {}

        def fake_wait(**kwargs):
            captured.update(kwargs)
            return "123456"

        with patch("app.services.browser_onboard.wait_for_mailbox_item", fake_wait):
            code = _wait_mailbox_code(**MAIL)
        self.assertEqual(code, "123456")
        self.assertEqual(captured["timeout_sec"], OTP_WAIT_SEC)
        self.assertEqual(OTP_WAIT_SEC, 60)
        self.assertEqual(OTP_RESEND_MAX, 2)

    def test_resend_stops_after_two_clicks(self):
        page = MagicMock()
        notes = []
        with patch("app.services.browser_onboard._click_resend_email", return_value=True):
            resends, ok = _resend_email_otp(page, resends=0, report=lambda stage, message: notes.append(message))
            self.assertTrue(ok)
            self.assertEqual(resends, 1)
            resends, ok = _resend_email_otp(page, resends=resends, report=lambda stage, message: notes.append(message))
            self.assertTrue(ok)
            self.assertEqual(resends, 2)
            resends, ok = _resend_email_otp(page, resends=resends, report=lambda stage, message: notes.append(message))
            self.assertFalse(ok)
            self.assertEqual(resends, 2)
        self.assertEqual(len(notes), 2)
        self.assertIn("第 1/2 次", notes[0])
        self.assertIn("第 2/2 次", notes[1])

    def test_wait_resends_twice_then_fails(self):
        page = MagicMock()
        waits = {"n": 0}
        clicks = {"n": 0}

        def fake_wait(**kwargs):
            waits["n"] += 1
            raise TimeoutError("email OTP timeout after 60s")

        def fake_click(page):
            clicks["n"] += 1
            return True

        with patch("app.services.browser_onboard.wait_for_mailbox_item", fake_wait), patch(
            "app.services.browser_onboard._click_resend_email", fake_click
        ), patch("app.services.browser_onboard._page_past_otp", return_value=False), patch(
            "app.services.browser_onboard._snapshot_mailbox_codes", return_value=set()
        ):
            resends = 0
            error = ""
            for _ in range(4):
                code, resends, error = wait_email_otp_with_resend(
                    page,
                    **MAIL,
                    ignore=set(),
                    resends=resends,
                    report=None,
                    check_past_otp=True,
                )
                if error:
                    break
                self.assertEqual(code, "")
            self.assertEqual(waits["n"], 3)
            self.assertEqual(clicks["n"], 2)
            self.assertEqual(resends, 2)
            self.assertIn("timeout after 60s", error)

    def test_wait_returns_code_without_resend(self):
        page = MagicMock()
        with patch("app.services.browser_onboard.wait_for_mailbox_item", return_value="654321"), patch(
            "app.services.browser_onboard._click_resend_email"
        ) as click:
            code, resends, error = wait_email_otp_with_resend(page, **MAIL, ignore=set(), resends=0)
        self.assertEqual(code, "654321")
        self.assertEqual(resends, 0)
        self.assertEqual(error, "")
        click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
