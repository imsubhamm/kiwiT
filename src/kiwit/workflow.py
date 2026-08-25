from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal

from .audit import HashChainAuditLog
from .domain import PortfolioSnapshot, Quote, RiskDecision, TradeProposal, WorkflowStatus
from .execution import PaperBroker, PaperFill
from .risk import RiskEngine


@dataclass(frozen=True)
class WorkflowState:
    status: WorkflowStatus
    proposal: TradeProposal
    quote: Quote
    portfolio: PortfolioSnapshot
    risk_decision: RiskDecision | None = None
    human_approved: bool = False
    fill: PaperFill | None = None


class PaperTradingWorkflow:
    def __init__(self, risk_engine: RiskEngine, broker: PaperBroker, audit: HashChainAuditLog) -> None:
        self.risk_engine = risk_engine
        self.broker = broker
        self.audit = audit

    def assess(self, state: WorkflowState, estimated_cost_per_unit: Decimal) -> WorkflowState:
        if state.status != WorkflowStatus.DATA_VALIDATED:
            raise ValueError("workflow must be data_validated before risk assessment")
        decision = self.risk_engine.evaluate(state.proposal, state.portfolio, estimated_cost_per_unit)
        status = WorkflowStatus.AWAITING_HUMAN if decision.quantity > 0 else WorkflowStatus.REJECTED
        self.audit.append("risk_decision", {"proposal_id": str(state.proposal.proposal_id), "decision": decision.decision, "reasons": decision.reason_codes, "quantity": decision.quantity})
        return replace(state, status=status, risk_decision=decision)

    def approve(self, state: WorkflowState) -> WorkflowState:
        if state.status != WorkflowStatus.AWAITING_HUMAN:
            raise ValueError("workflow is not awaiting human approval")
        self.audit.append("human_approval", {"proposal_id": str(state.proposal.proposal_id), "approved_at": datetime.now(UTC).isoformat()})
        return replace(state, status=WorkflowStatus.RISK_APPROVED, human_approved=True)

    def execute(self, state: WorkflowState) -> WorkflowState:
        if state.status != WorkflowStatus.RISK_APPROVED or not state.human_approved or state.risk_decision is None:
            raise ValueError("paper execution requires risk and human approval")
        fill = self.broker.execute(state.proposal, state.risk_decision, state.quote)
        self.audit.append("paper_fill", {"proposal_id": str(fill.proposal_id), "fill_id": str(fill.fill_id), "quantity": fill.quantity, "price": str(fill.price)})
        return replace(state, status=WorkflowStatus.EXECUTED, fill=fill)

