"""Invite seat intent contract: default omits seat_type; premium uses prolite."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from app.integrations.openai import member_adapter as adapter
from app.integrations.openai.chatgpt import ChatGPTClient
from app.integrations.openai.member_adapter import (
    InviteSeatIntent,
    build_invite_payload,
    classify_invite_submit_error,
    parse_invite_seat_intent,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "openai"


class InviteSeatContractTests(unittest.IsolatedAsyncioTestCase):
    def test_fixture_workspace_default_matches_builder(self):
        fixture = json.loads((FIXTURES / "invite_workspace_default_success.json").read_text(encoding="utf-8"))
        payload = build_invite_payload("invitee@example.com", role="owner", seat_intent="workspace_default")
        self.assertEqual(payload, fixture["request"]["json"])
        self.assertNotIn("seat_type", payload)

    def test_rejected_premium_fixture_classifies_stable_error(self):
        fixture = json.loads((FIXTURES / "invite_seat_type_rejected.json").read_text(encoding="utf-8"))
        classified = classify_invite_submit_error(
            status_code=fixture["response"]["status_code"],
            error=fixture["response"]["error_text"],
            error_body=fixture["response"]["structured"],
        )
        self.assertEqual(classified["code"], fixture["local_classification"]["code"])
        self.assertEqual(classified["retryable"], fixture["local_classification"]["retryable"])

    async def test_send_invite_default_omits_seat_type_and_sends_prolite_for_premium(self):
        client = ChatGPTClient()
        posted: list[dict] = []

        async def capture(method, url, headers, db_session=None, identifier="default", json_data=None, form_data=None):
            posted.append({"method": method, "url": url, "json": json_data})
            return {"success": True, "status_code": 200, "data": {"ok": True}}

        client._make_request = capture  # type: ignore[method-assign]
        ok = await client.send_invite("tok", "acc", "a@b.com", None, role="member")
        self.assertTrue(ok["success"])
        self.assertEqual(len(posted), 1)
        self.assertNotIn("seat_type", posted[0]["json"])

        posted.clear()
        prem = await client.send_invite("tok", "acc", "a@b.com", None, role="member", seat_intent="premium")
        self.assertTrue(prem["success"])
        self.assertEqual(posted[0]["json"].get("seat_type"), "prolite")
        self.assertEqual(prem.get("seat_intent"), "premium")

    async def test_invalid_seat_type_response_not_retried(self):
        client = ChatGPTClient()
        calls = {"n": 0}

        async def reject(method, url, headers, db_session=None, identifier="default", json_data=None, form_data=None):
            calls["n"] += 1
            return {
                "success": False,
                "status_code": 422,
                "error": "'premium' is not a valid SeatType",
                "error_code": None,
            }

        client._make_request = reject  # type: ignore[method-assign]
        original = dict(adapter.VERIFIED_INVITE_SEAT_WIRE_VALUES)
        adapter.VERIFIED_INVITE_SEAT_WIRE_VALUES[InviteSeatIntent.PREMIUM] = "premium"
        try:
            result = await client.send_invite(
                "token",
                "acc",
                "invitee@example.com",
                None,
                role="owner",
                seat_intent="premium",
            )
        finally:
            adapter.VERIFIED_INVITE_SEAT_WIRE_VALUES.clear()
            adapter.VERIFIED_INVITE_SEAT_WIRE_VALUES.update(original)

        self.assertFalse(result["success"])
        self.assertEqual(result["error_code"], "invite_seat_type_invalid")
        self.assertFalse(result.get("retryable"))
        self.assertEqual(calls["n"], 1)
        self.assertEqual(parse_invite_seat_intent("premium"), InviteSeatIntent.PREMIUM)

    def test_standard_intent_uses_default_wire(self):
        payload = build_invite_payload("kid@example.com", role="member", seat_intent=InviteSeatIntent.STANDARD)
        self.assertEqual(payload.get("seat_type"), "default")
        self.assertEqual(payload.get("role"), "standard-user")
        fixture = json.loads((FIXTURES / "invite_standard_default_success.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["request"]["json"]["seat_type"], "default")
        self.assertEqual(fixture["verified_mapping"]["standard_intent_wire_value"], "default")

    def test_premium_intent_uses_prolite_wire(self):
        payload = build_invite_payload("kid@example.com", role="owner", seat_intent=InviteSeatIntent.PREMIUM)
        self.assertEqual(payload.get("seat_type"), "prolite")
        self.assertEqual(payload.get("role"), "account-owner")
        fixture = json.loads((FIXTURES / "invite_premium_prolite_success.json").read_text(encoding="utf-8"))
        self.assertEqual(fixture["observed_invite"]["seat_type"], "prolite")
        self.assertEqual(fixture["verified_mapping"]["premium_intent_wire_value"], "prolite")


if __name__ == "__main__":
    unittest.main()
