import unittest

from app.legacy_sales import is_legacy_sales_path


class LegacySalesPathTests(unittest.TestCase):
    def test_blocks_sales_pages_and_apis(self):
        blocked = [
            "/redeem",
            "/redeem/verify",
            "/redeem/confirm",
            "/warranty/check",
            "/warranty/renewal-request",
            "/admin/welfare",
            "/admin/welfare/code/generate",
            "/admin/codes",
            "/admin/codes/generate",
            "/admin/records",
            "/admin/records/12/withdraw",
            "/admin/announcement",
            "/admin/renewal-requests",
            "/admin/renewal-requests/api",
            "/admin/teams/9/warranty-seat",
            "/admin/teams/batch-transfer-pool",
            "/admin/settings/warranty",
            "/admin/settings/warranty-auto-kick",
        ]
        for path in blocked:
            with self.subTest(path=path):
                self.assertTrue(is_legacy_sales_path(path), path)

    def test_keeps_ops_console(self):
        kept = [
            "/admin",
            "/admin/",
            "/admin/seats",
            "/admin/settings",
            "/admin/v2/",
            "/admin/teams/list",
            "/admin/teams/9/info",
            "/health",
            "/login",
        ]
        for path in kept:
            with self.subTest(path=path):
                self.assertFalse(is_legacy_sales_path(path), path)


if __name__ == "__main__":
    unittest.main()
