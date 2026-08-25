import sys
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.config import load_config
from kiwit.domain import Decision, Instrument, PortfolioSnapshot, Side, TradeProposal
from kiwit.risk import RiskEngine


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.engine = RiskEngine(load_config("config/kiwit.toml").risk)
        self.instrument = Instrument("NIFTYBEES")

    def proposal(self):
        return TradeProposal("baseline", "1", self.instrument, Side.BUY, datetime.now(UTC), Decimal("250"), Decimal("245"), None)

    def test_position_size_uses_fixed_fractional_risk(self):
        result = self.engine.evaluate(self.proposal(), PortfolioSnapshot(Decimal("1000000"), Decimal("1000000")), Decimal("0"))
        self.assertEqual(result.decision, Decision.APPROVE)
        self.assertEqual(result.quantity, 500)
        self.assertEqual(result.estimated_loss, Decimal("2500"))

    def test_drawdown_halt_fails_closed(self):
        portfolio = PortfolioSnapshot(Decimal("910000"), Decimal("1000000"))
        result = self.engine.evaluate(self.proposal(), portfolio, Decimal("0"))
        self.assertEqual(result.decision, Decision.REJECT)
        self.assertIn("DRAWDOWN_HALT", result.reason_codes)

    def test_daily_loss_limit_rejects_new_trade(self):
        portfolio = PortfolioSnapshot(Decimal("1000000"), Decimal("1000000"), realized_daily_pnl=Decimal("-2500"))
        result = self.engine.evaluate(self.proposal(), portfolio, Decimal("0"))
        self.assertEqual(result.decision, Decision.REJECT)
        self.assertIn("DAILY_LOSS_LIMIT", result.reason_codes)

