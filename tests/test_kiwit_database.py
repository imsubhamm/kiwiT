import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.database import DatabaseConfigurationError, DatabaseSettings, MigrationError, discover_migrations


class DatabaseTests(unittest.TestCase):
    def test_database_url_is_required(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(DatabaseConfigurationError):
            DatabaseSettings.from_env()

    def test_non_postgres_database_is_rejected(self):
        with (
            patch.dict(os.environ, {"KIWIT_DATABASE_URL": "sqlite:///unsafe.db"}, clear=True),
            self.assertRaises(DatabaseConfigurationError),
        ):
            DatabaseSettings.from_env()

    def test_discovers_ordered_checksummed_migrations(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "002_second.sql").write_text("SELECT 2;")
            (root / "001_first.sql").write_text("SELECT 1;")
            migrations = discover_migrations(root)
            self.assertEqual([item.version for item in migrations], [1, 2])
            self.assertEqual(len(migrations[0].checksum), 64)

    def test_invalid_migration_name_fails_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            Path(temp, "migration.sql").write_text("SELECT 1;")
            with self.assertRaises(MigrationError):
                discover_migrations(temp)

    def test_production_schema_contains_execution_safety_tables(self):
        root = Path(__file__).resolve().parents[1]
        sql = (root / "migrations" / "002_operational.sql").read_text()
        for table in ("trade_proposals", "risk_decisions", "broker_orders", "fills", "system_halts"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
        self.assertIn("audit_events_append_only", sql)


if __name__ == "__main__":
    unittest.main()
