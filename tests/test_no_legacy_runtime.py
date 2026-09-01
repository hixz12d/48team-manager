import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCAN_DIRS = (ROOT / "app", ROOT / "legacy_import")
TEXT_SUFFIXES = {".py", ".html", ".js", ".css", ".md"}
FORBIDDEN = (
    "admin_v2",
    "admin/v2",
    "admin/v3",
    "admin_v3",
    "/static/css/style.css",
    "/static/js/main.js",
    "redemption",
    "warranty",
    "welfare",
)


class NoLegacyRuntimeTests(unittest.TestCase):
    def test_runtime_does_not_keep_old_admin(self):
        hits = []
        for folder in SCAN_DIRS:
            for path in folder.rglob("*"):
                if not path.is_file() or path.suffix not in TEXT_SUFFIXES:
                    continue
                text = path.read_text(encoding="utf-8")
                for needle in FORBIDDEN:
                    if needle in text:
                        hits.append(f"{path.relative_to(ROOT)}:{needle}")
        self.assertEqual(hits, [])

    def test_legacy_import_is_not_runtime(self):
        main = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("legacy_import", main)
        self.assertNotIn("admin.router", main)
        self.assertNotIn("seats", main)
