import unittest
from unittest.mock import patch

from app.services.cloudflare_mail import (
    cloudflare_mail_client,
    normalize_cloudflare_base_url,
    parse_cloudflare_message,
)
from app.services.mail_otp import extract_code, extract_invite_url, parse_mail_line, wait_for_mailbox_item


class CloudflareMailTests(unittest.TestCase):
    def test_parse_mail_line_plain_icloud_uses_cloudflare(self):
        parsed = parse_mail_line("afraid-16.scepter@icloud.com")
        self.assertEqual(parsed["email"], "afraid-16.scepter@icloud.com")
        self.assertEqual(parsed["pickup_url"], "")
        self.assertTrue(parsed["use_cloudflare"])

    def test_parse_mail_line_keeps_pickup_url(self):
        parsed = parse_mail_line("a@icloud.com----https://pickup.example/show/x")
        self.assertFalse(parsed["use_cloudflare"])
        self.assertTrue(parsed["pickup_url"].startswith("https://"))

    def test_normalize_base_url(self):
        self.assertEqual(
            normalize_cloudflare_base_url("apimail.xiaozhudf2026.foo"),
            "https://apimail.xiaozhudf2026.foo",
        )

    def test_parse_rfc822_and_extract_code(self):
        raw = (
            "From: Service <noreply@example.test>\n"
            "To: icloud@example.test\n"
            "Subject: Verification code\n"
            "X-ICLOUD-HME: p=afraid-16.scepter@icloud.com; d=; f=icloud@example.test\n"
            "\n"
            "Your verification code is 654321\n"
        )
        message = parse_cloudflare_message({"id": 2, "raw": raw})
        self.assertEqual(message["to"], "afraid-16.scepter@icloud.com")
        self.assertIn("654321", message["preview"])

    def test_fetch_messages_sends_admin_header(self):
        raw = (
            "From: Service <noreply@example.test>\n"
            "To: icloud@example.test\n"
            "Subject: Verification code\n"
            "X-ICLOUD-HME: p=telefax-hay65@icloud.com\n"
            "\n"
            "Your verification code is 654321\n"
        )

        class FakeResponse:
            status_code = 200

            def json(self):
                return {"data": {"results": [{"id": 2, "raw": raw}]}}

        captured = {}

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get(self, url, params=None, headers=None):
                captured["url"] = url
                captured["params"] = params
                captured["headers"] = headers
                return FakeResponse()

        with patch("app.services.cloudflare_mail.httpx.Client", FakeClient):
            code = cloudflare_mail_client.find_code(
                base_url="https://apimail.xiaozhudf2026.foo",
                address="icloud@xiaozhudf2026.foo",
                admin_password="test-secret",
                alias="telefax-hay65@icloud.com",
            )
        self.assertEqual(code, "654321")
        self.assertTrue(captured["url"].endswith("/admin/mails"))
        self.assertEqual(captured["headers"]["x-admin-auth"], "test-secret")
        self.assertEqual(captured["params"]["address"], "icloud@xiaozhudf2026.foo")


class InviteUrlTests(unittest.TestCase):
    def test_prefers_invite_over_homepage(self):
        blob = "Visit https://chatgpt.com/ then https://chatgpt.com/invite/abc123"
        self.assertEqual(extract_invite_url(blob), "https://chatgpt.com/invite/abc123")

    def test_skips_bare_homepage_and_assets(self):
        blob = "Hello https://chatgpt.com/ logo https://chatgpt.com/favicon.ico"
        self.assertIsNone(extract_invite_url(blob))

    def test_auth_login_next(self):
        url = "https://chatgpt.com/auth/login?next=%2Forganization%2Faccept-invite"
        self.assertEqual(extract_invite_url(f"click {url}"), url)


class MailCodeTests(unittest.TestCase):
    def test_skips_invite_email_digits(self):
        blob = "Join workspace https://chatgpt.com/invite/abc123 id 123456"
        self.assertIsNone(extract_code(blob))

    def test_reads_verification_code(self):
        self.assertEqual(extract_code("Your verification code is 654321"), "654321")

    def test_openai_html_uses_isolated_code_not_theme_color(self):
        blob = """
        Subject: Your temporary ChatGPT verification code
        <html><head><title>Your temporary ChatGPT verification code</title>
        <style>.top{color:#202123}</style></head>
        <body>
          <div class="top" style="background-color: #ffffff;color:#202123;">
          <![endif]-->                          805572                          <!--[if mso]>
          </div>
        </body></html>
        """
        self.assertEqual(extract_code(blob), "805572")

    def test_ignores_hex_color_when_no_isolated_code(self):
        blob = "Your temporary ChatGPT verification code <div style=\"color:#202123\">hello</div>"
        self.assertIsNone(extract_code(blob))

    def test_wait_skips_ignored_code(self):
        def fake_list(**kwargs):
            return ["111111", "222222"]

        with patch("app.services.mail_otp.list_mailbox_codes", fake_list):
            found = wait_for_mailbox_item(
                email="a@b.com",
                pickup_url="https://pickup.example/show/x",
                proxy="http://127.0.0.1:1",
                kind="code",
                timeout_sec=5,
                ignore_values={"111111"},
            )
        self.assertEqual(found, "222222")

if __name__ == "__main__":
    unittest.main()
