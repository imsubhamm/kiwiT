import sys
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.persistence import DatasetRecord, LocalResearchStore


class PersistenceTests(unittest.TestCase):
    def test_dataset_registration_is_content_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            store = LocalResearchStore(Path(temp) / "research.db")
            record = DatasetRecord("test", "official_exchange", "https://example.test", "a" * 64, 2, datetime.now(UTC), date(2024, 1, 1), date(2024, 1, 2))
            self.assertEqual(store.register_dataset(record), store.register_dataset(record))
            store.close()

    def test_backtest_fingerprint_is_immutable(self):
        with tempfile.TemporaryDirectory() as temp:
            store = LocalResearchStore(Path(temp) / "research.db")
            dataset = DatasetRecord("test", "official_exchange", "https://example.test", "b" * 64, 2, datetime.now(UTC))
            dataset_id = store.register_dataset(dataset)
            store.register_strategy("s", "1", "research", {"rule": "x"})
            now = datetime.now(UTC)
            store.record_backtest("s", "1", (dataset_id,), "c" * 64, {"p": 1}, {"return": 2}, now, now)
            with self.assertRaises(ValueError):
                store.record_backtest("s", "1", (dataset_id,), "c" * 64, {"p": 1}, {"return": 999}, now, now)
            store.close()

    def test_strategy_version_cannot_change_specification(self):
        with tempfile.TemporaryDirectory() as temp:
            store = LocalResearchStore(Path(temp) / "research.db")
            store.register_strategy("s", "1", "research", {"rule": "x"})
            with self.assertRaises(ValueError):
                store.register_strategy("s", "1", "research", {"rule": "changed"})
            store.close()
