import sys
import unittest
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.promotion import (
    MANDATORY_EVIDENCE_GATES,
    PromotedStrategy,
    PromotedStrategyCatalog,
    PromotionEvidence,
    approval_now,
)
from kiwit.scheduling import UnattendedPaperScheduler


class SchedulingTests(unittest.TestCase):
    def test_unpromoted_strategy_keeps_unattended_run_locked(self):
        submitted = []
        scheduler = UnattendedPaperScheduler(
            PromotedStrategyCatalog(), "regime_router", "1.0.0", lambda _: ["signal"], submitted.append
        )
        result = scheduler.run(datetime(2026, 8, 25, 11, 0, tzinfo=UTC))
        self.assertEqual(result.state, "locked")
        self.assertEqual(submitted, [])

    def test_promoted_strategy_runs_once_and_submits_only_to_paper_callback(self):
        catalog = PromotedStrategyCatalog()
        catalog.register(PromotedStrategy(
            "passing_fixture", "1", {"test": True},
            PromotionEvidence("a" * 64, "b" * 64, {gate: True for gate in MANDATORY_EVIDENCE_GATES}),
            approval_now("test-committee", "test-only unanimous evidence fixture"),
        ))
        submitted = []
        scheduler = UnattendedPaperScheduler(catalog, "passing_fixture", "1", lambda _: ["a", "b"], submitted.append)
        now = datetime(2026, 8, 25, 11, 0, tzinfo=UTC)
        self.assertEqual(scheduler.run(now).state, "completed")
        self.assertEqual(scheduler.run(now).state, "already_completed")
        self.assertEqual(submitted, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
