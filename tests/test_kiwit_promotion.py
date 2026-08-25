import sys
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.config import load_config
from kiwit.domain import Instrument, PortfolioSnapshot, Side, TradeProposal
from kiwit.promotion import (
    MANDATORY_EVIDENCE_GATES,
    PromotedStrategy,
    PromotedStrategyCatalog,
    PromotionEvidence,
    StrategyApproval,
)
from kiwit.risk import RiskEngine


def passing_evidence() -> PromotionEvidence:
    return PromotionEvidence(
        "a" * 64,
        "b" * 64,
        {gate: True for gate in MANDATORY_EVIDENCE_GATES},
    )


class StrategyPromotionTests(unittest.TestCase):
    def setUp(self):
        self.approval = StrategyApproval(
            "risk-committee", "all frozen evidence gates independently reviewed", datetime.now(UTC)
        )

    def test_failed_gate_cannot_be_promoted(self):
        gates = {gate: True for gate in MANDATORY_EVIDENCE_GATES}
        gates["median_trade_after_costs"] = False
        with self.assertRaisesRegex(ValueError, "median_trade_after_costs"):
            PromotedStrategy(
                "ema_cross_sectional", "2", {"fast": 20, "slow": 100},
                PromotionEvidence("a" * 64, "b" * 64, gates), self.approval,
            )

    def test_approval_record_is_mandatory(self):
        with self.assertRaises(ValueError):
            StrategyApproval("", "", datetime.now(UTC))

    def test_promoted_version_is_immutable(self):
        catalog = PromotedStrategyCatalog()
        first = PromotedStrategy("candidate", "1", {"lookback": 20}, passing_evidence(), self.approval)
        changed = PromotedStrategy("candidate", "1", {"lookback": 21}, passing_evidence(), self.approval)
        catalog.register(first)
        with self.assertRaisesRegex(ValueError, "immutable"):
            catalog.register(changed)

    def test_unpromoted_version_is_denied(self):
        with self.assertRaises(PermissionError):
            PromotedStrategyCatalog().require_promoted("ema_cross_sectional", "2")

    def test_risk_sizing_is_deterministic_for_identical_inputs(self):
        proposal = TradeProposal(
            "candidate", "1", Instrument("NIFTYBEES"), Side.BUY, datetime.now(UTC),
            Decimal(250), Decimal(245), None,
        )
        portfolio = PortfolioSnapshot(Decimal(1000000), Decimal(1000000))
        engine = RiskEngine(load_config("config/kiwit.toml").risk)
        first = engine.evaluate(proposal, portfolio, Decimal("0.10"))
        second = engine.evaluate(proposal, portfolio, Decimal("0.10"))
        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()
