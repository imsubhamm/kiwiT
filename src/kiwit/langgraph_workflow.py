from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal, TypedDict
from uuid import UUID

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from .audit import HashChainAuditLog
from .domain import (
    Decision,
    Instrument,
    PortfolioSnapshot,
    Quote,
    RiskDecision,
    Side,
    TradeProposal,
    WorkflowStatus,
)
from .execution import PaperFill
from .promotion import PromotedStrategyCatalog
from .risk import RiskEngine


class TradingGraphState(TypedDict, total=False):
    proposal: dict[str, Any]
    quote: dict[str, Any]
    portfolio: dict[str, Any]
    estimated_cost_per_unit: str
    status: str
    risk_decision: dict[str, Any]
    human_review: dict[str, Any]
    fill: dict[str, Any]
    message: str


def _instrument(data: dict[str, Any]) -> Instrument:
    return Instrument(
        symbol=data["symbol"], exchange=data.get("exchange", "NSE"), series=data.get("series", "EQ"),
        lot_size=int(data.get("lot_size", 1)), tick_size=Decimal(data.get("tick_size", "0.05")),
    )


def _proposal(data: dict[str, Any]) -> TradeProposal:
    return TradeProposal(
        strategy_id=data["strategy_id"], strategy_version=data["strategy_version"], instrument=_instrument(data["instrument"]),
        side=Side(data["side"]), signal_timestamp=datetime.fromisoformat(data["signal_timestamp"]),
        entry_price=Decimal(data["entry_price"]), stop_price=Decimal(data["stop_price"]),
        target_price=Decimal(data["target_price"]) if data.get("target_price") is not None else None,
        rationale=data.get("rationale", {}), proposal_id=UUID(data["proposal_id"]),
    )


def _quote(data: dict[str, Any]) -> Quote:
    return Quote(
        _instrument(data["instrument"]), datetime.fromisoformat(data["timestamp"]),
        Decimal(data["bid"]), Decimal(data["ask"]), Decimal(data["last"]),
    )


def _portfolio(data: dict[str, Any]) -> PortfolioSnapshot:
    return PortfolioSnapshot(
        equity=Decimal(data["equity"]), high_watermark=Decimal(data["high_watermark"]),
        realized_daily_pnl=Decimal(data.get("realized_daily_pnl", "0")),
        realized_weekly_pnl=Decimal(data.get("realized_weekly_pnl", "0")),
        positions=(), correlated_open_risk=Decimal(data.get("correlated_open_risk", "0")),
    )


def _risk(data: dict[str, Any]) -> RiskDecision:
    return RiskDecision(
        Decision(data["decision"]), UUID(data["proposal_id"]), int(data["quantity"]),
        Decimal(data["risk_budget"]), Decimal(data["estimated_loss"]), tuple(data["reason_codes"]),
    )


def _risk_dict(decision: RiskDecision) -> dict[str, Any]:
    return {
        "decision": decision.decision.value, "proposal_id": str(decision.proposal_id), "quantity": decision.quantity,
        "risk_budget": str(decision.risk_budget), "estimated_loss": str(decision.estimated_loss),
        "reason_codes": list(decision.reason_codes),
    }


def _fill_dict(fill: PaperFill) -> dict[str, Any]:
    return {
        "fill_id": str(fill.fill_id), "proposal_id": str(fill.proposal_id), "timestamp": fill.timestamp.isoformat(),
        "side": fill.side.value, "quantity": fill.quantity, "price": str(fill.price), "environment": "paper",
    }


def graph_input(
    proposal: TradeProposal,
    quote: Quote,
    portfolio: PortfolioSnapshot,
    estimated_cost_per_unit: Decimal,
) -> TradingGraphState:
    instrument = {
        "symbol": proposal.instrument.symbol, "exchange": proposal.instrument.exchange, "series": proposal.instrument.series,
        "lot_size": proposal.instrument.lot_size, "tick_size": str(proposal.instrument.tick_size),
    }
    return {
        "proposal": {
            "proposal_id": str(proposal.proposal_id), "strategy_id": proposal.strategy_id,
            "strategy_version": proposal.strategy_version, "instrument": instrument, "side": proposal.side.value,
            "signal_timestamp": proposal.signal_timestamp.isoformat(), "entry_price": str(proposal.entry_price),
            "stop_price": str(proposal.stop_price),
            "target_price": str(proposal.target_price) if proposal.target_price is not None else None,
            "rationale": dict(proposal.rationale),
        },
        "quote": {
            "instrument": instrument, "timestamp": quote.timestamp.isoformat(), "bid": str(quote.bid),
            "ask": str(quote.ask), "last": str(quote.last),
        },
        "portfolio": {
            "equity": str(portfolio.equity), "high_watermark": str(portfolio.high_watermark),
            "realized_daily_pnl": str(portfolio.realized_daily_pnl),
            "realized_weekly_pnl": str(portfolio.realized_weekly_pnl),
            "correlated_open_risk": str(portfolio.correlated_open_risk),
        },
        "estimated_cost_per_unit": str(estimated_cost_per_unit),
        "status": WorkflowStatus.RECEIVED.value,
    }


def build_paper_trading_graph(
    risk_engine: RiskEngine,
    broker: Any,
    audit: HashChainAuditLog,
    checkpointer: Any,
    promoted_strategies: PromotedStrategyCatalog,
    *,
    maximum_quote_age_seconds: int = 15,
) -> Any:
    if maximum_quote_age_seconds < 1:
        raise ValueError("maximum quote age must be positive")

    def validate_data(state: TradingGraphState) -> TradingGraphState:
        proposal = _proposal(state["proposal"])
        quote = _quote(state["quote"])
        now = datetime.now(UTC)
        quote_time = quote.timestamp.astimezone(UTC)
        if not promoted_strategies.is_promoted(proposal.strategy_id, proposal.strategy_version):
            return {"status": WorkflowStatus.REJECTED.value, "message": "STRATEGY_NOT_PROMOTED"}
        if quote.instrument != proposal.instrument:
            return {"status": WorkflowStatus.REJECTED.value, "message": "QUOTE_INSTRUMENT_MISMATCH"}
        if quote_time > now or (now - quote_time).total_seconds() > maximum_quote_age_seconds:
            return {"status": WorkflowStatus.REJECTED.value, "message": "STALE_OR_FUTURE_QUOTE"}
        if proposal.signal_timestamp.astimezone(UTC) > quote_time:
            return {"status": WorkflowStatus.REJECTED.value, "message": "SIGNAL_AFTER_QUOTE"}
        return {"status": WorkflowStatus.DATA_VALIDATED.value}

    def route_after_validation(state: TradingGraphState) -> Literal["assess_risk", "reject"]:
        return "assess_risk" if state["status"] == WorkflowStatus.DATA_VALIDATED.value else "reject"

    def assess_risk(state: TradingGraphState) -> TradingGraphState:
        proposal = _proposal(state["proposal"])
        decision = risk_engine.evaluate(proposal, _portfolio(state["portfolio"]), Decimal(state["estimated_cost_per_unit"]))
        audit.append_once(
            f"risk:{proposal.proposal_id}", "risk_decision",
            {"proposal_id": str(proposal.proposal_id), "decision": decision.decision.value,
             "reasons": list(decision.reason_codes), "quantity": decision.quantity},
        )
        status = WorkflowStatus.AWAITING_HUMAN if decision.decision == Decision.APPROVE else WorkflowStatus.REJECTED
        return {"status": status.value, "risk_decision": _risk_dict(decision)}

    def route_after_risk(state: TradingGraphState) -> Literal["human_review", "reject"]:
        return "human_review" if state["status"] == WorkflowStatus.AWAITING_HUMAN.value else "reject"

    def human_review(state: TradingGraphState) -> Command[Literal["execute_paper", "reject"]]:
        decision = interrupt(
            {
                "action": "review_trade", "proposal": state["proposal"], "risk_decision": state["risk_decision"],
                "warning": "Approval authorizes PAPER execution only.",
            }
        )
        if not isinstance(decision, dict) or not isinstance(decision.get("approved"), bool):
            return Command(update={"status": WorkflowStatus.REJECTED.value, "message": "INVALID_HUMAN_RESPONSE"}, goto="reject")
        review = {
            "approved": decision["approved"], "reviewer": str(decision.get("reviewer", "unknown")),
            "reason": str(decision.get("reason", "")), "reviewed_at": datetime.now(UTC).isoformat(),
        }
        proposal_id = state["proposal"]["proposal_id"]
        audit.append_once(f"human:{proposal_id}", "human_review", {"proposal_id": proposal_id, **review})
        if not review["approved"]:
            return Command(update={"human_review": review, "status": WorkflowStatus.REJECTED.value}, goto="reject")
        return Command(update={"human_review": review, "status": WorkflowStatus.RISK_APPROVED.value}, goto="execute_paper")

    def execute_paper(state: TradingGraphState) -> TradingGraphState:
        if state.get("status") != WorkflowStatus.RISK_APPROVED.value or not state.get("human_review", {}).get("approved"):
            return {"status": WorkflowStatus.REJECTED.value, "message": "APPROVAL_REQUIRED"}
        proposal = _proposal(state["proposal"])
        existing = broker.fill_for(proposal.proposal_id)
        fill = existing or broker.execute(proposal, _risk(state["risk_decision"]), _quote(state["quote"]))
        audit.append_once(
            f"fill:{proposal.proposal_id}", "paper_fill",
            {"proposal_id": str(proposal.proposal_id), "fill_id": str(fill.fill_id),
             "quantity": fill.quantity, "price": str(fill.price)},
        )
        return {"status": WorkflowStatus.EXECUTED.value, "fill": _fill_dict(fill)}

    def reject(state: TradingGraphState) -> TradingGraphState:
        proposal_id = state["proposal"]["proposal_id"]
        audit.append_once(
            f"reject:{proposal_id}", "workflow_rejected",
            {"proposal_id": proposal_id, "reason": state.get("message", "RISK_OR_HUMAN_REJECTION")},
        )
        return {"status": WorkflowStatus.REJECTED.value}

    builder = StateGraph(TradingGraphState)
    builder.add_node("validate_data", validate_data)
    builder.add_node("assess_risk", assess_risk)
    builder.add_node("human_review", human_review)
    builder.add_node("execute_paper", execute_paper)
    builder.add_node("reject", reject)
    builder.add_edge(START, "validate_data")
    builder.add_conditional_edges("validate_data", route_after_validation)
    builder.add_conditional_edges("assess_risk", route_after_risk)
    builder.add_edge("execute_paper", END)
    builder.add_edge("reject", END)
    return builder.compile(checkpointer=checkpointer)
