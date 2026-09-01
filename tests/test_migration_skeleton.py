import tempfile
import unittest
from pathlib import Path

from legacy_import.importer import inspect_legacy_db


class MigrationSkeletonTests(unittest.TestCase):
    def test_missing_legacy_db_is_read_only_report(self):
        report = inspect_legacy_db(Path("/tmp/does-not-exist-team-manage.db"))
        self.assertTrue(report.dry_run)
        self.assertIn("legacy database file not found", report.notes)

    def test_existing_file_is_not_modified(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "team_manage.db"
            path.write_bytes(b"legacy")
            before = path.read_bytes()
            report = inspect_legacy_db(path)
            self.assertEqual(path.read_bytes(), before)
            self.assertTrue(report.dry_run)
            self.assertTrue(any("skeleton" in note for note in report.notes))
