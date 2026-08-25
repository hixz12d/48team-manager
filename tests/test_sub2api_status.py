import unittest
from datetime import datetime, timedelta, timezone

from app.services.sub2api import Sub2ApiService


class Sub2ApiStatusTests(unittest.TestCase):
    def setUp(self):
        self.service = Sub2ApiService()

    def test_groups_team_accounts_by_family(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=6)).isoformat()
        boxes = self.service.group_accounts([
            {
                "id": 1,
                "name": "Team .2026.15 母号",
                "status": "active",
                "schedulable": False,
                "credentials": {"email": "xiaozhudf.2026.15@gmail.com"},
                "extra": {"codex_7d_used_percent": 81},
            },
            {
                "id": 2,
                "name": "Team .2026.15 子号 1",
                "status": "error",
                "schedulable": False,
                "error_message": "Token revoked (401): Encountered invalidated oauth token",
                "credentials": {"email": "eagle-snoop-4d@icloud.com"},
                "extra": {"codex_7d_used_percent": 100},
            },
            {
                "id": 3,
                "name": "Team 2026.27 子号 2",
                "status": "active",
                "schedulable": True,
                "rate_limited_at": "2026-08-24T14:47:42+08:00",
                "rate_limit_reset_at": future,
                "credentials": {"email": "lava_tuning_4w@icloud.com"},
                "extra": {"codex_7d_used_percent": 100},
            },
            {
                "id": 4,
                "name": "Team Pedro 母号",
                "status": "active",
                "schedulable": True,
                "credentials": {"email": "pedropick89@gmail.com"},
                "extra": {"codex_7d_used_percent": 18},
            },
        ])

        titles = [box["title"] for box in boxes]
        self.assertEqual(titles, [".2026.15", ".2026.27", "Pedro"])

        fifteen = boxes[0]["accounts"]
        self.assertEqual([row["short_name"] for row in fifteen], ["母号", "子号 1"])
        self.assertEqual(fifteen[0]["quota_label"], "7日 81%")
        self.assertEqual(fifteen[0]["account_cost_label"], "")
        self.assertEqual(fifteen[0]["schedule_label"], "不调度")
        self.assertEqual(fifteen[1]["schedule"], "401")
        self.assertEqual(boxes[1]["accounts"][0]["schedule"], "429")
        self.assertEqual(boxes[2]["accounts"][0]["short_name"], "母号")

    def test_keeps_email_named_accounts_without_team_prefix(self):
        account = {
            "id": 9,
            "name": "xiaozhudf2026.28@gmail.com",
            "status": "active",
            "schedulable": True,
            "credentials": {"email": "xiaozhudf2026.28@gmail.com"},
            "extra": {"codex_7d_used_percent": 50},
        }
        self.assertTrue(self.service._is_relevant_account(account))
        boxes = self.service.group_accounts([account])
        self.assertEqual(boxes[0]["title"], ".2026.28")
        self.assertEqual(boxes[0]["accounts"][0]["short_name"], "母号")

    def test_apply_window_costs_uses_seven_day_stats(self):
        row = {"id": 12}
        self.service.apply_window_costs(row, {
            "five_hour": {"window_stats": {"cost": 9.99, "user_cost": 8.88}},
            "seven_day": {"window_stats": {"cost": 1.234, "user_cost": 2.5}},
        })
        self.assertEqual(row["account_cost_label"], "A $1.23")
        self.assertEqual(row["user_cost_label"], "U $2.50")

    def test_apply_window_costs_ignores_five_hour_fallback(self):
        row = {"id": 13}
        self.service.apply_window_costs(row, {
            "five_hour": {"window_stats": {"cost": 9.99, "user_cost": 8.88}},
            "window_stats": {"cost": 3, "user_cost": 4},
        })
        self.assertEqual(row["account_cost_label"], "")
        self.assertEqual(row["user_cost_label"], "")

    def test_annotate_cost_totals_sums_group_and_grand(self):
        boxes = [
            {
                "title": ".2026.21",
                "accounts": [
                    {"account_cost": 108.97, "user_cost": 12.76},
                    {"account_cost": 96.79, "user_cost": 11.21},
                ],
            },
            {
                "title": "Pedro",
                "accounts": [
                    {"account_cost": "10.50", "user_cost": None},
                    {"account_cost": None, "user_cost": 1.5},
                ],
            },
        ]
        grand = self.service.annotate_cost_totals(boxes)
        self.assertEqual(boxes[0]["account_cost_label"], "A $205.76")
        self.assertEqual(boxes[0]["user_cost_label"], "U $23.97")
        self.assertEqual(boxes[1]["account_cost_label"], "A $10.50")
        self.assertEqual(boxes[1]["user_cost_label"], "U $1.50")
        self.assertEqual(grand["account_cost_label"], "A $216.26")
        self.assertEqual(grand["user_cost_label"], "U $25.47")

    def test_indexes_status_by_email(self):
        boxes = self.service.group_accounts([
            {
                "id": 12,
                "name": "Team 2026.28 子号 3",
                "status": "error",
                "error_message": "Token revoked (401)",
                "credentials": {"email": "phoebes-likely-69@icloud.com"},
                "extra": {"codex_7d_used_percent": 89},
            }
        ])
        index = self.service.index_status_by_email(boxes)
        row = index["phoebes-likely-69@icloud.com"]
        self.assertEqual(row["quota_label"], "7日 89%")
        self.assertEqual(row["schedule"], "401")

    def test_annotates_today_rotation_from_live_members(self):
        boxes = self.service.group_accounts([
            {
                "id": 1,
                "name": "Team .2026.15 母号",
                "status": "active",
                "credentials": {"email": "xiaozhudf.2026.15@gmail.com"},
            },
            {
                "id": 4,
                "name": "Team Pedro 母号",
                "status": "active",
                "credentials": {"email": "pedropick89@gmail.com"},
            },
        ])
        now = datetime(2026, 8, 24, 21, 0, 0)
        self.service.annotate_rotation(boxes, [
            {
                "email": "xiaozhudf.2026.15@gmail.com",
                "team_name": "Dual World",
                "live_members": [
                    {"email": "xiaozhudf.2026.15@gmail.com", "role": "account-owner", "joined_at": "2026-08-24T01:00:00+00:00"},
                    {"email": "eagle-snoop-4d@icloud.com", "role": "standard-user", "joined_at": "2026-08-24T04:10:00+00:00"},
                ],
            },
            {
                "email": "pedropick89@gmail.com",
                "team_name": "SunshineRain",
                "live_members": [
                    {"email": "old-seat@icloud.com", "role": "standard-user", "joined_at": "2026-08-23T10:00:00+00:00"},
                ],
            },
        ], now=now)
        by_title = {box["title"]: box for box in boxes}
        self.assertTrue(by_title[".2026.15"]["rotated_today"])
        self.assertEqual(by_title[".2026.15"]["rotation_label"], "今日已轮 1次")
        self.assertEqual(by_title[".2026.15"]["rotation_count"], 1)
        self.assertFalse(by_title["Pedro"]["rotated_today"])
        self.assertEqual(by_title["Pedro"]["rotation_label"], "今日未轮")
        self.assertEqual(by_title["Pedro"]["rotation_tone"], "warn")

    def test_annotates_rotation_from_kicked_member_events(self):
        boxes = self.service.group_accounts([
            {
                "id": 4,
                "name": "Team Pedro 母号",
                "status": "active",
                "credentials": {"email": "pedropick89@gmail.com"},
            },
        ])
        now = datetime(2026, 8, 24, 21, 0, 0)
        self.service.annotate_rotation(boxes, [
            {
                "id": 5,
                "email": "pedropick89@gmail.com",
                "team_name": "SunshineRain",
                "live_members": [
                    {"email": "pedropick89@gmail.com", "role": "account-owner", "joined_at": "2026-08-20T01:00:00+00:00"},
                ],
                "rotation_emails": ["think_midsole.9e@icloud.com"],
                "rotation_count": 1,
            },
        ], now=now)
        box = boxes[0]
        self.assertTrue(box["rotated_today"])
        self.assertEqual(box["rotation_count"], 1)
        self.assertEqual(box["rotation_label"], "今日已轮 1次")
        self.assertEqual(box["rotation_tone"], "ok")

    def test_manual_rotation_count_overrides_auto_for_today(self):
        boxes = self.service.group_accounts([
            {
                "id": 4,
                "name": "Team Pedro 母号",
                "status": "active",
                "credentials": {"email": "pedropick89@gmail.com"},
            },
        ])
        now = datetime(2026, 8, 24, 21, 0, 0)
        self.service.annotate_rotation(boxes, [
            {
                "id": 5,
                "email": "pedropick89@gmail.com",
                "team_name": "SunshineRain",
                "rotation_emails": ["old@icloud.com", "new@icloud.com"],
                "rotation_manual_count": 1,
                "rotation_manual_on": "2026-08-24",
            },
        ], now=now)
        box = boxes[0]
        self.assertEqual(box["team_id"], 5)
        self.assertEqual(box["rotation_auto_count"], 2)
        self.assertTrue(box["rotation_manual"])
        self.assertEqual(box["rotation_count"], 1)
        self.assertEqual(box["rotation_label"], "今日已轮 1次")

    def test_stale_manual_rotation_count_is_ignored(self):
        boxes = self.service.group_accounts([
            {
                "id": 4,
                "name": "Team Pedro 母号",
                "status": "active",
                "credentials": {"email": "pedropick89@gmail.com"},
            },
        ])
        now = datetime(2026, 8, 24, 21, 0, 0)
        self.service.annotate_rotation(boxes, [
            {
                "id": 5,
                "email": "pedropick89@gmail.com",
                "team_name": "SunshineRain",
                "rotation_emails": ["old@icloud.com", "new@icloud.com"],
                "rotation_manual_count": 9,
                "rotation_manual_on": "2026-08-23",
            },
        ], now=now)
        box = boxes[0]
        self.assertFalse(box["rotation_manual"])
        self.assertEqual(box["rotation_auto_count"], 2)
        self.assertEqual(box["rotation_count"], 2)
        self.assertEqual(box["rotation_label"], "今日已轮 2次")


if __name__ == "__main__":
    unittest.main()
