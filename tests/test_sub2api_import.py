import unittest

from app.services.sub2api import Sub2ApiService


TEAM_TEMPLATE = {
    "id": "tmpl-team",
    "name": "Team轮转",
    "platform": "openai",
    "type": "oauth",
    "is_default": False,
    "include_groups": True,
    "values": {
        "proxy_id": None,
        "concurrency": 3,
        "priority": 1,
        "rate_multiplier": 1,
        "group_ids": [38, 39, 40, 41, 112, 114, 5],
        "auto_pause_on_expired": True,
        "openai_ws_mode": "passthrough",
        "openai_compact_mode": "auto",
        "codex_fingerprint_mode": "window40",
        "tls_fingerprint_enabled": True,
        "tls_fingerprint_profile_id": None,
    },
}


class Sub2ApiImportTests(unittest.TestCase):
    def setUp(self):
        self.service = Sub2ApiService()

    def test_picks_named_openai_oauth_template(self):
        picked = self.service.pick_account_create_template(
            [
                {"name": "Free模板", "platform": "openai", "type": "oauth", "is_default": False},
                TEAM_TEMPLATE,
                {"name": "Team轮转", "platform": "anthropic", "type": "oauth"},
            ],
            name="Team轮转",
        )
        self.assertEqual(picked["id"], "tmpl-team")

    def test_template_fields_keep_groups_and_drop_proxy(self):
        fields = self.service.template_import_fields(TEAM_TEMPLATE, [112])
        self.assertEqual(fields["group_ids"], [38, 39, 40, 41, 112, 114, 5])
        self.assertEqual(fields["concurrency"], 3)
        extra = fields["extra"]
        self.assertEqual(extra["openai_oauth_responses_websockets_v2_mode"], "passthrough")
        self.assertTrue(extra["openai_oauth_responses_websockets_v2_enabled"])
        self.assertEqual(extra["codex_fingerprint_mode"], "window40")
        self.assertTrue(extra["enable_tls_fingerprint"])
        self.assertNotIn("proxy_id", extra)

    def test_missing_template_falls_back_to_group_ids(self):
        fields = self.service.template_import_fields(None, [112])
        self.assertEqual(fields["group_ids"], [112])
        self.assertIsNone(fields["extra"])

    def test_matches_proxy_by_host_port(self):
        proxies = [
            {"id": 31, "name": "Team .2026.15", "host": "207.97.151.218", "port": 443},
            {"id": 32, "name": "Team pedropick", "host": "69.3.236.236", "port": 443},
        ]
        proxy_id = self.service.match_proxy_id(
            proxies,
            "socks5h://user:pass@207.97.151.218:443",
        )
        self.assertEqual(proxy_id, 31)

    def test_proxy_falls_back_to_family_siblings(self):
        proxy_id = self.service.match_proxy_id(
            [{"id": 30, "host": "216.151.254.5", "port": 443}],
            "",
            siblings=[
                {"id": 1, "proxy_id": 30},
                {"id": 2, "proxy": {"id": 30}},
                {"id": 3, "proxy_id": None},
            ],
        )
        self.assertEqual(proxy_id, 30)

    def test_family_labels_preserve_live_naming(self):
        self.assertEqual(self.service.derive_family_label("xiaozhudf.2026.15@gmail.com"), ".2026.15")
        self.assertEqual(self.service.derive_family_label("xiaozhudf2026.27@gmail.com"), "2026.27")
        self.assertEqual(self.service.derive_family_label("pedropick89@gmail.com"), "Pedro")
        self.assertEqual(self.service.derive_family_label("newxiaozhu2@gmail.com"), "new2")

        accounts = [
            {"id": 1, "name": "Team 2026.27 母号", "credentials": {"email": "xiaozhudf2026.27@gmail.com"}},
            {"id": 2, "name": "Team 2026.27 子号 4", "credentials": {"email": "jogger-chick.7s@icloud.com"}},
        ]
        self.assertEqual(
            self.service.display_family_label(accounts, "xiaozhudf2026.27@gmail.com"),
            "2026.27",
        )
        self.assertEqual(
            self.service.build_account_name(
                role="child",
                email="fresh-seat@icloud.com",
                team_email="xiaozhudf2026.27@gmail.com",
                accounts=accounts,
            ),
            "Team 2026.27 子号 5",
        )
        self.assertEqual(
            self.service.build_account_name(
                role="child",
                email="jogger-chick.7s@icloud.com",
                team_email="xiaozhudf2026.27@gmail.com",
                accounts=accounts,
            ),
            "Team 2026.27 子号 4",
        )
        self.assertEqual(
            self.service.build_account_name(
                role="owner",
                email="xiaozhudf.2026.15@gmail.com",
                team_email="xiaozhudf.2026.15@gmail.com",
                accounts=[],
            ),
            "Team .2026.15 母号",
        )

    def test_create_child_payload_applies_template_and_proxy(self):
        fields = self.service.template_import_fields(TEAM_TEMPLATE, [112])
        payload = self.service.build_codex_import_payload(
            content="tok",
            name="Team .2026.15 子号 4",
            role="child",
            existing=None,
            template_fields=fields,
            fallback_group_ids=[112],
            proxy_id=31,
        )
        self.assertEqual(payload["name"], "Team .2026.15 子号 4")
        self.assertEqual(payload["group_ids"], [38, 39, 40, 41, 112, 114, 5])
        self.assertEqual(payload["proxy_id"], 31)
        self.assertEqual(payload["concurrency"], 3)
        self.assertTrue(payload["confirm_mixed_channel_risk"])
        self.assertEqual(payload["extra"]["codex_fingerprint_mode"], "window40")

    def test_update_only_fills_missing_proxy_and_groups(self):
        fields = self.service.template_import_fields(TEAM_TEMPLATE, [112])
        existing = {
            "id": 2866,
            "name": "Team .2026.15 子号 1",
            "group_ids": [112],
            "proxy_id": None,
        }
        payload = self.service.build_codex_import_payload(
            content="tok",
            name="Team .2026.15 子号 1",
            role="child",
            existing=existing,
            template_fields=fields,
            fallback_group_ids=[112],
            proxy_id=31,
        )
        self.assertEqual(payload["proxy_id"], 31)
        self.assertNotIn("group_ids", payload)
        self.assertNotIn("extra", payload)
        self.assertNotIn("concurrency", payload)

        already_bound = self.service.build_codex_import_payload(
            content="tok",
            name="Team .2026.15 子号 3",
            role="child",
            existing={"id": 2885, "group_ids": [112, 41], "proxy_id": 31},
            template_fields=fields,
            fallback_group_ids=[112],
            proxy_id=31,
        )
        self.assertNotIn("proxy_id", already_bound)
        self.assertNotIn("group_ids", already_bound)

    def test_owner_create_does_not_use_rotation_groups(self):
        fields = self.service.template_import_fields(TEAM_TEMPLATE, [112])
        payload = self.service.build_codex_import_payload(
            content="tok",
            name="Team Pedro 母号",
            role="owner",
            existing=None,
            template_fields=fields,
            fallback_group_ids=[112],
            proxy_id=32,
        )
        self.assertEqual(payload["group_ids"], [112])
        self.assertEqual(payload["proxy_id"], 32)
        self.assertNotIn("extra", payload)


if __name__ == "__main__":
    unittest.main()
