import unittest
from unittest.mock import AsyncMock, patch

from app.services.chatgpt import ChatGPTService


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data or {}
        self.text = text or ""

    def json(self):
        return self._json_data


class FakeCurlSession:
    def __init__(self):
        self.closed = False
        self.calls = []

    async def get(self, url, headers=None):
        self.calls.append(("GET", url, headers or {}))
        return FakeResponse(json_data={"items": [], "total": 0})

    async def post(self, url, headers=None, json=None):
        self.calls.append(("POST", url, headers or {}))
        return FakeResponse(json_data={})

    async def delete(self, url, headers=None, json=None):
        self.calls.append(("DELETE", url, headers or {}))
        return FakeResponse(json_data={})

    async def close(self):
        self.closed = True


class TransientThenOkSession:
    def __init__(self, label):
        self.label = label
        self.closed = False
        self.calls = 0

    async def get(self, url, headers=None):
        self.calls += 1
        if self.label == "old":
            raise RuntimeError("Failed to perform, curl: (28) Connection timed out after 30001 milliseconds")
        return FakeResponse(json_data={"ok": True})

    async def close(self):
        self.closed = True


class ChatGPTCloudflareHeaderTests(unittest.IsolatedAsyncioTestCase):
    def test_default_referer_matches_admin_tabs(self):
        service = ChatGPTService()
        self.assertEqual(
            service._default_referer("https://chatgpt.com/backend-api/accounts/abc/users?limit=50"),
            ChatGPTService.ADMIN_MEMBERS_REFERER,
        )
        self.assertEqual(
            service._default_referer("https://chatgpt.com/backend-api/accounts/abc/invites?offset=0"),
            ChatGPTService.ADMIN_INVITES_REFERER,
        )
        self.assertEqual(
            service._default_referer("https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"),
            "https://chatgpt.com/",
        )

    def test_transient_transport_errors_are_detected(self):
        self.assertTrue(ChatGPTService._is_transient_transport_error(
            "failed to perform, curl: (35) boringssl ssl_connect: connection closed abruptly"
        ))
        self.assertTrue(ChatGPTService._is_transient_transport_error(
            "curl: (28) connection timed out after 30001 milliseconds"
        ))
        self.assertFalse(ChatGPTService._is_transient_transport_error("account_deactivated"))

    async def test_make_request_adds_admin_and_oai_headers(self):
        service = ChatGPTService()
        session = FakeCurlSession()
        service._sessions["owner@example.com"] = session

        result = await service._make_request(
            "GET",
            "https://chatgpt.com/backend-api/accounts/acc-1/users?offset=0&limit=50&query=",
            {"Authorization": "Bearer token", "chatgpt-account-id": "acc-1"},
            identifier="owner@example.com",
        )

        self.assertTrue(result["success"])
        headers = session.calls[0][2]
        self.assertEqual(headers["chatgpt-account-id"], "acc-1")
        self.assertEqual(headers["oai-client-version"], ChatGPTService.OAI_CLIENT_VERSION)
        self.assertEqual(headers["oai-language"], "zh-CN")
        self.assertEqual(headers["Referer"], ChatGPTService.ADMIN_MEMBERS_REFERER)
        self.assertTrue(headers["oai-device-id"])
        self.assertEqual(headers["oai-device-id"], service._device_id_for("owner@example.com"))

    async def test_timeout_rebuilds_session_before_retry(self):
        service = ChatGPTService()
        old_session = TransientThenOkSession("old")
        new_session = TransientThenOkSession("new")
        service._sessions["owner@example.com"] = old_session
        service.RETRY_DELAYS = [0, 0, 0]

        async def fake_create(_db_session, identifier="default"):
            self.assertEqual(identifier, "owner@example.com")
            return new_session

        with patch.object(service, "_create_session", new=AsyncMock(side_effect=fake_create)):
            result = await service._make_request(
                "GET",
                "https://chatgpt.com/backend-api/accounts/acc-1/users?offset=0&limit=50&query=",
                {"Authorization": "Bearer token", "chatgpt-account-id": "acc-1"},
                db_session=object(),
                identifier="owner@example.com",
            )

        self.assertTrue(result["success"])
        self.assertTrue(old_session.closed)
        self.assertEqual(new_session.calls, 1)
        self.assertIs(service._sessions["owner@example.com"], new_session)

    async def test_delete_404_is_treated_as_already_removed(self):
        service = ChatGPTService()
        session = FakeCurlSession()
        original_delete = session.delete

        async def delete_404(url, headers=None, json=None):
            await original_delete(url, headers=headers, json=json)
            return FakeResponse(status_code=404, json_data={"detail": "not found"}, text="not found")

        session.delete = delete_404
        service._sessions["owner@example.com"] = session

        result = await service._make_request(
            "DELETE",
            "https://chatgpt.com/backend-api/accounts/acc-1/users/user-abc",
            {"Authorization": "Bearer token", "chatgpt-account-id": "acc-1"},
            identifier="owner@example.com",
        )

        self.assertTrue(result["success"])
        self.assertTrue(result.get("already_removed"))

    def test_already_removed_markers(self):
        self.assertTrue(ChatGPTService.is_already_removed_error(404, "whatever"))
        self.assertTrue(ChatGPTService.is_already_removed_error(400, "User is not a member of this workspace"))
        self.assertFalse(ChatGPTService.is_already_removed_error(400, "account_deactivated", "account_deactivated"))

    async def test_get_wham_usage_hits_official_path(self):
        service = ChatGPTService()
        session = FakeCurlSession()
        service._sessions["owner@icloud.com"] = session
        result = await service.get_wham_usage(
            "tok",
            object(),
            account_id="acct-official",
            identifier="owner@icloud.com",
        )
        self.assertTrue(result["success"])
        self.assertEqual(session.calls[0][1], "https://chatgpt.com/backend-api/wham/usage")
        self.assertEqual(session.calls[0][2]["chatgpt-account-id"], "acct-official")


if __name__ == "__main__":
    unittest.main()
