"""Official Workspace name extraction: match by ID, never take the first item."""

from __future__ import annotations

import unittest

from app.domain.workspaces.metadata import extract_title_for_workspace, redact_payload, resolve_official_title
from app.domain.workspaces.names import apply_custom_name, apply_official_name, resolve_display_name
from app.persistence.models.identity import Workspace

WS = "e61b2377-aaaa-bbbb-cccc-ddddeeee0001"
OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeffff0002"


class WorkspaceMetadataTests(unittest.TestCase):
    def test_top_level_title_for_matching_id(self):
        payload = {"id": WS, "title": "运营一组"}
        found = extract_title_for_workspace(payload, WS)
        self.assertEqual(found["title"], "运营一组")

    def test_nested_account_workspace_team(self):
        payload = {
            "accounts": [
                {"id": OTHER, "name": "别人的团队"},
                {"id": WS, "account": {"workspace": {"name": "嵌套名称"}}},
            ]
        }
        found = extract_title_for_workspace(payload, WS)
        self.assertEqual(found["title"], "嵌套名称")

    def test_does_not_pick_first_array_item(self):
        payload = {
            "organizations": [
                {"id": OTHER, "title": "先出现的别人"},
                {"id": WS, "title": "目标团队"},
            ]
        }
        found = extract_title_for_workspace(payload, WS)
        self.assertEqual(found["title"], "目标团队")

    def test_empty_and_email_names_are_rejected(self):
        self.assertIsNone(extract_title_for_workspace({"id": WS, "title": ""}, WS)["title"])
        self.assertIsNone(extract_title_for_workspace({"id": WS, "name": "owner@example.com"}, WS, owner_email="owner@example.com")["title"])

    def test_custom_name_survives_official_change(self):
        workspace = Workspace(official_workspace_id=WS, name=None)
        apply_custom_name(workspace, "我的组")
        apply_official_name(workspace, "官方后来改了", payload_source="account_context")
        display = resolve_display_name(workspace)
        self.assertEqual(display["display_name"], "我的组")
        self.assertEqual(display["name_source"], "custom")
        self.assertEqual(display["official_name"], "官方后来改了")

    def test_schema_change_or_failure_keeps_old_name(self):
        workspace = Workspace(official_workspace_id=WS, official_name="已有官方名", name="已有官方名", name_source="official")
        resolved = resolve_official_title([("broken", {"unexpected": True})], WS)
        self.assertIsNone(resolved["title"])
        apply_official_name(workspace, None, last_error=resolved["error"])
        display = resolve_display_name(workspace)
        self.assertEqual(display["display_name"], "已有官方名")
        self.assertTrue(display["official_name_last_error"])

    def test_redact_drops_tokens_and_emails(self):
        redacted = redact_payload({"access_token": "secret", "email": "a@b.com", "accounts": {"id": WS}})
        self.assertEqual(redacted["access_token"], "<redacted>")
        self.assertEqual(redacted["email"], "<redacted_email>")
