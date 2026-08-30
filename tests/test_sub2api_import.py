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

    def test_matches_proxy_by_family_name(self):
        proxy_id = self.service.match_proxy_id(
            [
                {"id": 35, "name": "Team new1", "host": "38.248.197.171", "port": 443},
                {"id": 33, "name": "Team new2", "host": "66.80.132.187", "port": 443},
            ],
            "",
            family_label="new1",
        )
        self.assertEqual(proxy_id, 35)

    def test_classifies_phone_and_http_probes(self):
        self.assertEqual(self.service.classify_probe(error="Phone verification required")["kind"], "phone")
        self.assertEqual(self.service.classify_probe(status_code=401, error="revoked")["kind"], "401")
        self.assertEqual(self.service.classify_probe(status_code=403, error="forbidden")["kind"], "403")
        self.assertEqual(self.service.classify_probe(status_code=200, payload={"ok": True})["kind"], "200")

    def test_builds_next_free_account_name(self):
        accounts = [
            {"id": 1, "name": "Free 1", "credentials": {"email": "a@icloud.com"}},
            {"id": 2, "name": "Free 27", "credentials": {"email": "b@icloud.com"}},
            {"id": 3, "name": "Free 26", "credentials": {"email": "c@icloud.com"}},
        ]
        self.assertEqual(self.service.build_free_account_name(email="fresh@icloud.com", accounts=accounts), "Free 28")
        self.assertEqual(self.service.build_free_account_name(email="b@icloud.com", accounts=accounts), "Free 27")

    def test_picks_free_template(self):
        picked = self.service.pick_account_create_template(
            [
                {"name": "Team轮转", "platform": "openai", "type": "oauth", "id": "team"},
                {"name": "Free模板", "platform": "openai", "type": "oauth", "id": "free"},
            ],
            name="Free模板",
        )
        self.assertEqual(picked["id"], "free")

    def test_finds_owner_when_401_wipes_email(self):
        unnamed_owner = {
            "id": 2894,
            "name": "Team new1 母号",
            "credentials": {},
            "error_message": "Token revoked (401)",
        }
        child = {
            "id": 2914,
            "name": "Team new1 子号 2",
            "credentials": {"email": "wings_pubs_1i@icloud.com"},
        }
        self.assertIsNone(
            self.service.find_existing_account(
                [unnamed_owner, child],
                email="newxiaozhu1@gmail.com",
                role="owner",
                team_email="newxiaozhu1@gmail.com",
            )
        )
        self.assertEqual(
            self.service.find_existing_account(
                [unnamed_owner, child],
                email="newxiaozhu1@gmail.com",
                existing_id=2894,
                role="owner",
                team_email="newxiaozhu1@gmail.com",
            )["id"],
            2894,
        )
        self.assertEqual(
            self.service.find_existing_account(
                [unnamed_owner, child],
                email="wings_pubs_1i@icloud.com",
                role="child",
                team_email="newxiaozhu1@gmail.com",
            )["id"],
            2914,
        )

    def test_finds_unnamed_child_in_family(self):
        unnamed_child = {
            "id": 2924,
            "name": "Team new2 子号 1",
            "credentials": {},
        }
        owner = {
            "id": 2887,
            "name": "Team new2 母号",
            "credentials": {"email": "newxiaozhu2@gmail.com"},
        }
        self.assertIsNone(
            self.service.find_existing_account(
                [owner, unnamed_child],
                email="13.each-curl@icloud.com",
                role="child",
                team_email="newxiaozhu2@gmail.com",
            )
        )

    def test_matches_official_id_before_email(self):
        by_official = {
            "id": 11,
            "name": "乱名",
            "credentials": {"chatgpt_account_id": "acct-official", "email": "other@icloud.com"},
        }
        by_email = {
            "id": 12,
            "name": "Team xxx 母号",
            "credentials": {"email": "owner@icloud.com"},
        }
        self.assertEqual(
            self.service.find_existing_account(
                [by_email, by_official],
                email="owner@icloud.com",
                chatgpt_account_id="acct-official",
            )["id"],
            11,
        )

    def test_gmail_name_is_not_bound_as_owner(self):
        gmail = {
            "id": 70,
            "name": "Team xxx 母号",
            "credentials": {"email": "pro.user@gmail.com"},
        }
        self.assertIsNone(
            self.service.find_existing_account(
                [gmail],
                email="owner@icloud.com",
                role="owner",
                team_email="owner@icloud.com",
            )
        )
    def test_oauth_probe_200_opens_schedulable(self):
        self.assertTrue(self.service.should_open_schedulable_after_oauth_probe({"kind": "200", "label": "200"}))
        self.assertFalse(self.service.should_open_schedulable_after_oauth_probe({"kind": "401", "label": "401"}))
        self.assertFalse(self.service.should_open_schedulable_after_oauth_probe({"kind": "phone"}))
        self.assertFalse(self.service.should_open_schedulable_after_oauth_probe({}))



if __name__ == "__main__":
    unittest.main()
