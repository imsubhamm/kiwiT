import sys
import unittest
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.operations import build_operational_report


class OperationalReviewTests(unittest.TestCase):
    def test_equity_drawdown_and_incident_review(self):
        report = build_operational_report(
            account_id="paper", initial_cash=Decimal("100000"), cash_balance=Decimal("98000"),
            realized_pnl=Decimal("-2000"),
            daily_rows=[
                (date(2026, 8, 20), Decimal("100000"), Decimal("10"), Decimal("1000"), 1),
                (date(2026, 8, 21), Decimal("95000"), Decimal("12"), Decimal("1200"), 2),
                (date(2026, 8, 22), Decimal("98000"), Decimal("0"), Decimal("0"), 0),
            ],
            incident_rows=[("paper", "STALE_DATA", "feed stale", False, datetime.now(UTC), datetime.now(UTC))],
            positions=1,
        )
        self.assertEqual(report["summary"]["max_drawdown_pct"], "-5.0000")
        self.assertEqual(report["summary"]["trade_count"], 3)
        self.assertEqual(report["summary"]["active_incidents"], 0)
        self.assertFalse(report["automation"]["enabled"])

    def test_active_incident_blocks_review(self):
        report = build_operational_report(
            account_id="paper", initial_cash=Decimal("100"), cash_balance=Decimal("100"), realized_pnl=Decimal("0"),
            daily_rows=[], incident_rows=[("global", "DB_OUTAGE", "down", True, datetime.now(UTC), None)], positions=0,
        )
        self.assertEqual(report["status"], "blocked")
        self.assertFalse(report["review"]["checks"][2]["passed"])
