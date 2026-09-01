import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.db_migrations import run_auto_migration


class LegacySalesDropTests(unittest.TestCase):
    def test_drops_redemption_tables_and_normalizes_pool(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(db_path)
            conn.executescript(
                """
                CREATE TABLE teams (
                    id INTEGER PRIMARY KEY,
                    email VARCHAR(255),
                    access_token_encrypted TEXT,
                    pool_type VARCHAR(20)
                );
                INSERT INTO teams (id, pool_type) VALUES (1, 'welfare'), (2, 'normal');
                CREATE TABLE redemption_codes (code VARCHAR(32) PRIMARY KEY);
                CREATE TABLE redemption_records (id INTEGER PRIMARY KEY, code VARCHAR(32));
                CREATE TABLE renewal_requests (id INTEGER PRIMARY KEY, code VARCHAR(32));
                CREATE TABLE settings (key VARCHAR(100) PRIMARY KEY, value TEXT);
                INSERT INTO settings (key, value) VALUES
                    ('warranty_auto_kick_enabled', 'true'),
                    ('welfare_common_code', 'ABC'),
                    ('free_account_proxy', '');
                """
            )
            conn.commit()
            conn.close()

            run_auto_migration(db_path)

            conn = sqlite3.connect(db_path)
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            pools = [row[0] for row in conn.execute("SELECT pool_type FROM teams ORDER BY id")]
            keys = [row[0] for row in conn.execute("SELECT key FROM settings ORDER BY key")]
            conn.close()

            self.assertNotIn("redemption_codes", tables)
            self.assertNotIn("redemption_records", tables)
            self.assertNotIn("renewal_requests", tables)
            self.assertEqual(pools, ["normal", "normal"])
            self.assertEqual(keys, ["free_account_proxy"])


if __name__ == "__main__":
    unittest.main()
