import sys
import tempfile
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kiwit.audit import HashChainAuditLog
from kiwit.config import load_config
from kiwit.domain import Instrument, PortfolioSnapshot, Quote, Side, TradeProposal, WorkflowStatus
from kiwit.execution import PaperBroker
from kiwit.risk import RiskEngine
from kiwit.workflow import PaperTradingWorkflow, WorkflowState


class WorkflowTests(unittest.TestCase):
    def test_risk_human_and_paper_execution_gates(self):
        instrument = Instrument("NIFTYBEES")
        proposal = TradeProposal("baseline", "1", instrument, Side.BUY, datetime.now(UTC), Decimal("250"), Decimal("245"), None)
        quote = Quote(instrument, datetime.now(UTC), Decimal("249.95"), Decimal("250.05"), Decimal("250"))
        portfolio = PortfolioSnapshot(Decimal("1000000"), Decimal("1000000"))
        with tempfile.TemporaryDirectory() as temp:
            audit = HashChainAuditLog(Path(temp) / "audit.jsonl")
            workflow = PaperTradingWorkflow(RiskEngine(load_config("config/kiwit.toml").risk), PaperBroker(), audit)
            state = WorkflowState(WorkflowStatus.DATA_VALIDATED, proposal, quote, portfolio)
            state = workflow.assess(state, Decimal("0.10"))
            self.assertEqual(state.status, WorkflowStatus.AWAITING_HUMAN)
            with self.assertRaises(ValueError):
                workflow.execute(state)
            state = workflow.approve(state)
            state = workflow.execute(state)
            self.assertEqual(state.status, WorkflowStatus.EXECUTED)
            self.assertTrue(audit.verify())

