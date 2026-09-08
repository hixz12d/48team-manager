"""Credential sync helper: recovery modes and safe degradation messaging."""

from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from app.application.sub2api_credential_sync import (
    assess_auth_error,
    choose_recovery_mode,
    present_sync_message,
    sync_bound_oauth_credentials,
)


class CredentialSyncHelperTests(unittest.IsolatedAsyncioTestCase):
    def test_assess_auth_error_conservative(self):
        self.assertFalse(assess_auth_error({"status": "error", "error_message": ""}))
        self.assertTrue(assess_auth_error({"status": "error", "error_message": "oauth 401 unauthorized"}))
        self.assertFalse(assess_auth_error({"status": "error", "error_message": "rate limit only"}))

    def test_choose_recovery_mode(self):
        remote = {"status": "error", "error_message": "token is expired"}
        self.assertEqual(
            choose_recovery_mode("background_refresh", remote=remote, auth_validated=True),
            "credentials_only",
        )
        self.assertEqual(
            choose_recovery_mode("manual_reauthorize", remote=remote, auth_validated=True),
            "auth_only",
        )
        self.assertEqual(
            choose_recovery_mode("manual_reauthorize", remote=remote, auth_validated=False),
            "credentials_only",
        )

    def test_present_message_separates_write_and_pause(self):
        message = present_sync_message(
            {
                "remote_account_id": 2980,
                "credential_write": "succeeded",
                "auth_recovery": "cleared",
                "token_cache_invalidation": "succeeded",
                "schedulable": False,
            }
        )
        self.assertIn("2980", message)
        self.assertIn("旧授权错误已清除", message)
        self.assertIn("调度开关仍关闭", message)

    async def test_unsupported_narrow_interface_falls_back_without_clear_error(self):
        remote = {
            "id": 55, "platform": "openai", "type": "oauth",
            "status": "error",
            "error_message": "401 unauthorized",
            "email": "kid@example.com",
            "schedulable": False,
        }
        after = {
            "id": 55, "platform": "openai", "type": "oauth",
            "status": "error",
            "error_message": "401 unauthorized",
            "email": "kid@example.com",
            "schedulable": False,
        }
        client = AsyncMock()
        client.sync_oauth_credentials = AsyncMock(
            return_value={"ok": False, "supported": False, "error_code": "sync_oauth_unsupported"}
        )
        client.update_account = AsyncMock(return_value={"id": 55})
        client.read_after_write = AsyncMock(return_value=after)
        client.get_account = AsyncMock(return_value=remote)
        with patch("app.application.sub2api_credential_sync.sub2api_client", client):
            result = await sync_bound_oauth_credentials(
                db=AsyncMock(),
                remote_id=55,
                credentials={"access_token": "new"},
                expected_email="kid@example.com",
                reason="manual_reauthorize",
                auth_validated=True,
                prefetched_remote=remote,
            )
        self.assertTrue(result["ok"])
        self.assertFalse(result["supported"])
        self.assertEqual(result["auth_recovery"], "skipped")
        self.assertTrue(result["partial"])
        client.update_account.assert_awaited()


if __name__ == "__main__":
    unittest.main()
