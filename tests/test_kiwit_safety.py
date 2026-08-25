import json
import sys
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.audit import HashChainAuditLog
from kiwit.domain import Decision, Instrument, Quote, RiskDecision, Side, TradeProposal
from kiwit.execution import PaperBroker
from kiwit.strategy import StrategyContext, TrendPullbackResearchStrategy


class SafetyTests(unittest.TestCase):
    def test_rejected_strategy_cannot_emit_proposal(self):
        strategy = TrendPullbackResearchStrategy()
        context = StrategyContext(datetime.now(UTC), Instrument("NIFTYBEES"), {})
        self.assertEqual(strategy.metadata.status, "rejected")
        self.assertIsNone(strategy.evaluate(context))

    def test_paper_broker_rejects_duplicate_proposal(self):
        instrument = Instrument("NIFTYBEES")
        proposal = TradeProposal("baseline", "1", instrument, Side.BUY, datetime.now(UTC), Decimal("250"), Decimal("245"), None)
        risk = RiskDecision(Decision.APPROVE, proposal.proposal_id, 10, Decimal("50"), Decimal("50"), ())
        quote = Quote(instrument, datetime.now(UTC), Decimal("249.95"), Decimal("250.05"), Decimal("250"))
        broker = PaperBroker()
        broker.execute(proposal, risk, quote)
        with self.assertRaises(ValueError):
            broker.execute(proposal, risk, quote)

    def test_audit_tampering_is_detected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "audit.jsonl"
            audit = HashChainAuditLog(path)
            audit.append("one", {"value": 1})
            record = json.loads(path.read_text())
            record["payload"]["value"] = 2
            path.write_text(json.dumps(record) + "\n")
            self.assertFalse(audit.verify())


if __name__ == "__main__":
    unittest.main()
