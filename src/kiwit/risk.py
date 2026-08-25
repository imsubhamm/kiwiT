from __future__ import annotations

from decimal import Decimal, ROUND_FLOOR

from .config import RiskConfig
from .domain import Decision, PortfolioSnapshot, RiskDecision, TradeProposal, ZERO


class RiskEngine:
    """Deterministic, fail-closed position sizing and portfolio risk checks."""

    def __init__(self, config: RiskConfig) -> None:
        self.config = config

    def evaluate(self, proposal: TradeProposal, portfolio: PortfolioSnapshot, estimated_cost_per_unit: Decimal) -> RiskDecision:
        reasons: list[str] = []
        if portfolio.equity <= ZERO:
            reasons.append("NON_POSITIVE_EQUITY")
        if len(portfolio.positions) >= self.config.maximum_positions:
            reasons.append("MAXIMUM_POSITIONS")
        if portfolio.drawdown_fraction >= self.config.drawdown_halt:
            reasons.append("DRAWDOWN_HALT")

        base_risk = portfolio.equity * self.config.risk_per_trade
        if portfolio.drawdown_fraction >= self.config.drawdown_throttle:
            base_risk /= Decimal("2")

        if portfolio.realized_daily_pnl <= -(base_risk * self.config.daily_loss_limit_r):
            reasons.append("DAILY_LOSS_LIMIT")
        if portfolio.realized_weekly_pnl <= -(base_risk * self.config.weekly_loss_limit_r):
            reasons.append("WEEKLY_LOSS_LIMIT")

        per_unit_risk = abs(proposal.entry_price - proposal.stop_price) + estimated_cost_per_unit
        if per_unit_risk <= ZERO:
            reasons.append("INVALID_UNIT_RISK")
            quantity = 0
        else:
            raw_quantity = (base_risk / per_unit_risk).to_integral_value(rounding=ROUND_FLOOR)
            lots = int(raw_quantity) // proposal.instrument.lot_size
            quantity = lots * proposal.instrument.lot_size
            if quantity < proposal.instrument.lot_size:
                reasons.append("BELOW_MINIMUM_LOT")

        estimated_loss = per_unit_risk * quantity
        current_open_risk = sum((position.open_risk for position in portfolio.positions), ZERO)
        if current_open_risk + estimated_loss > portfolio.equity * self.config.maximum_open_risk:
            reasons.append("MAXIMUM_OPEN_RISK")
        if portfolio.correlated_open_risk + estimated_loss > portfolio.equity * self.config.maximum_correlated_risk:
            reasons.append("MAXIMUM_CORRELATED_RISK")

        decision = Decision.REJECT if reasons else Decision.APPROVE
        return RiskDecision(decision, proposal.proposal_id, quantity if not reasons else 0, base_risk, estimated_loss, tuple(reasons))

