import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from kiwit.audit import HashChainAuditLog
from kiwit.config import load_config
from kiwit.domain import Instrument, PortfolioSnapshot, Quote, Side, TradeProposal, WorkflowStatus
from kiwit.execution import PaperBroker
from kiwit.langgraph_workflow import build_paper_trading_graph, graph_input
from kiwit.risk import RiskEngine


class LangGraphWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.instrument = Instrument("NIFTYBEES")
        self.now = datetime.now(UTC)
        self.proposal = TradeProposal(
            "baseline", "1", self.instrument, Side.BUY, self.now - timedelta(seconds=2),
            Decimal(250), Decimal(245), None,
        )
        self.quote = Quote(self.instrument, self.now, Decimal("249.95"), Decimal("250.05"), Decimal(250))
        self.portfolio = PortfolioSnapshot(Decimal(1000000), Decimal(1000000))

    def build(self, temp: str):
        audit = HashChainAuditLog(Path(temp) / "graph-audit.jsonl")
        broker = PaperBroker()
        graph = build_paper_trading_graph(
            RiskEngine(load_config("config/kiwit.toml").risk), broker, audit, InMemorySaver(), maximum_quote_age_seconds=60,
        )
        return graph, broker, audit

    def test_graph_interrupts_then_resumes_approved_paper_fill(self):
        with tempfile.TemporaryDirectory() as temp:
            graph, _, audit = self.build(temp)
            config = {"configurable": {"thread_id": "approved"}}
            first = graph.invoke(graph_input(self.proposal, self.quote, self.portfolio, Decimal("0.10")), config=config)
            self.assertIn("__interrupt__", first)
            self.assertEqual(first["status"], WorkflowStatus.AWAITING_HUMAN.value)
            final = graph.invoke(Command(resume={"approved": True, "reviewer": "tester"}), config=config)
            self.assertEqual(final["status"], WorkflowStatus.EXECUTED.value)
            self.assertEqual(final["fill"]["environment"], "paper")
            self.assertTrue(audit.verify())

    def test_human_rejection_never_executes(self):
        with tempfile.TemporaryDirectory() as temp:
            graph, broker, _ = self.build(temp)
            config = {"configurable": {"thread_id": "rejected"}}
            graph.invoke(graph_input(self.proposal, self.quote, self.portfolio, Decimal("0.10")), config=config)
            final = graph.invoke(Command(resume={"approved": False, "reviewer": "tester"}), config=config)
            self.assertEqual(final["status"], WorkflowStatus.REJECTED.value)
            self.assertIsNone(broker.fill_for(self.proposal.proposal_id))

    def test_stale_quote_fails_before_interrupt(self):
        with tempfile.TemporaryDirectory() as temp:
            graph, broker, _ = self.build(temp)
            stale = Quote(
                self.instrument, self.now - timedelta(minutes=10), Decimal("249.95"), Decimal("250.05"), Decimal(250)
            )
            proposal = TradeProposal(
                "baseline", "1", self.instrument, Side.BUY, self.now - timedelta(minutes=11),
                Decimal(250), Decimal(245), None,
            )
            final = graph.invoke(
                graph_input(proposal, stale, self.portfolio, Decimal("0.10")),
                config={"configurable": {"thread_id": "stale"}},
            )
            self.assertEqual(final["status"], WorkflowStatus.REJECTED.value)
            self.assertNotIn("__interrupt__", final)
            self.assertIsNone(broker.fill_for(proposal.proposal_id))


if __name__ == "__main__":
    unittest.main()
