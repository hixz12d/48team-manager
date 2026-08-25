import unittest
from datetime import datetime, timedelta

from app.services.team_view import present_occupancy, present_risks


class TeamViewTests(unittest.TestCase):
    def test_occupancy_uses_local_cap_as_operation_limit(self):
        view = present_occupancy({"current_members": 4, "max_members": 6})
        self.assertEqual(view["occupied"], 4)
        self.assertEqual(view["capacity"], 6)
        self.assertEqual(view["occupancy_text"], "4/6")
        self.assertEqual(view["capacity_label"], "操作上限")
        self.assertFalse(view["over_capacity"])

    def test_unknown_capacity_stays_unknown(self):
        view = present_occupancy({"current_members": 2, "max_members": 0})
        self.assertIsNone(view["capacity"])
        self.assertEqual(view["occupancy_text"], "2/未知")
        self.assertIsNone(view["available"])

    def test_live_members_split_joined_and_invited(self):
        view = present_occupancy({
            "current_members": 3,
            "max_members": 6,
            "live_members": [
                {"email": "a@x.com", "status": "joined"},
                {"email": "b@x.com", "status": "invited"},
                {"email": "owner@x.com", "status": "joined"},
            ],
        })
        self.assertEqual(view["upstream_joined"], 2)
        self.assertEqual(view["upstream_invited"], 1)
        self.assertEqual(view["upstream_occupied"], 3)

    def test_risks_include_held_seat_and_expiry(self):
        now = datetime(2026, 3, 20, 12, 0, 0)
        risks = present_risks(
            {
                "status": "active",
                "current_members": 3,
                "max_members": 5,
                "expires_at": (now + timedelta(days=2)).isoformat(),
                "vacancy": {"is_free": False, "safe_to_refill": False},
            },
            now=now,
        )
        keys = {item["key"] for item in risks["items"]}
        self.assertIn("expiring_soon", keys)
        self.assertIn("seat_held", keys)
        self.assertEqual(risks["level"], "warning")

    def test_banned_is_critical(self):
        risks = present_risks({"status": "banned", "current_members": 1, "max_members": 5})
        self.assertEqual(risks["level"], "critical")
        self.assertEqual(risks["label"], "管理号封禁")


if __name__ == "__main__":
    unittest.main()
