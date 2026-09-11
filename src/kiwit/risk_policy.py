"""Versioned mandatory paper-risk gates, independent of ML and LLM configuration."""

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .config import RiskConfig
from .domain import Decision, Side
from .marketdata.canonical import IST, TradingCalendar, utc
from .ml.datasets import _hash
from .risk import RiskEngine

VERSION = "hard-risk-policy-v1"


def _encoded(value):
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): _encoded(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [_encoded(v) for v in value]
    return value


@dataclass(frozen=True)
class HardLimits:
    maximum_quantity: int = 1000
    maximum_trades_per_day: int = 10
    loss_streak_limit: int = 3
    loss_cooldown_seconds: int = 1800
    no_entry_before_close_seconds: int = 900
    maximum_state_age_seconds: int = 30
    emergency_disabled: bool = True

    def __post_init__(self):
        for name, value in asdict(self).items():
            if name == "emergency_disabled":
                if type(value) is not bool:
                    raise ValueError("Emergency flag must be boolean")
            elif type(value) is not int or value <= 0:
                raise ValueError("Limits must be positive integers")


@dataclass(frozen=True)
class RiskState:
    as_of: datetime
    trading_day: str
    trades_today: int
    consecutive_losses: int
    last_loss_at: datetime | None = None


@dataclass(frozen=True)
class DeterministicRiskPolicy:
    risk: RiskConfig
    limits: HardLimits
    calendar: TradingCalendar

    def __post_init__(self):
        for name, value in asdict(self.risk).items():
            if name == "maximum_positions":
                if type(value) is not int or value <= 0:
                    raise ValueError("Invalid position limit")
            elif not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("Invalid risk configuration")

    def evaluate(self, proposal, portfolio, estimated_cost_per_unit, *, state: RiskState, now, audit):
        now = utc(now)
        reasons = []
        if self.limits.emergency_disabled:
            reasons.append("EMERGENCY_DISABLED")
        session = self.calendar.session(now.astimezone(IST).date())
        if session is None:
            reasons.append("UNKNOWN_OR_CLOSED_SESSION")
        else:
            opened, closed = session.bounds()
            if not opened <= now < closed - timedelta(seconds=self.limits.no_entry_before_close_seconds):
                reasons.append("OUTSIDE_ENTRY_WINDOW")
        try:
            if not 0 <= (now - utc(state.as_of)).total_seconds() <= self.limits.maximum_state_age_seconds:
                reasons.append("STALE_OR_FUTURE_RISK_STATE")
            if state.trading_day != now.astimezone(IST).date().isoformat():
                reasons.append("RISK_STATE_DAY_MISMATCH")
            if any(type(v) is not int or v < 0 for v in (state.trades_today, state.consecutive_losses)):
                raise ValueError("Invalid counters")
            if state.trades_today >= self.limits.maximum_trades_per_day:
                reasons.append("MAXIMUM_DAILY_TRADES")
            if state.consecutive_losses >= self.limits.loss_streak_limit and (
                state.last_loss_at is None
                or now - utc(state.last_loss_at) < timedelta(seconds=self.limits.loss_cooldown_seconds)
            ):
                reasons.append("CONSECUTIVE_LOSS_COOLDOWN")
            if state.last_loss_at is not None and utc(state.last_loss_at) > now:
                reasons.append("FUTURE_LOSS_STATE")
        except (ValueError, TypeError):
            reasons.append("INVALID_RISK_STATE")
        quantity, budget, loss = 0, Decimal(0), Decimal(0)
        try:
            values = [
                portfolio.equity,
                portfolio.high_watermark,
                portfolio.realized_daily_pnl,
                portfolio.realized_weekly_pnl,
                portfolio.correlated_open_risk,
                estimated_cost_per_unit,
                proposal.entry_price,
                proposal.stop_price,
            ]
            if any(not isinstance(v, Decimal) or not v.is_finite() for v in values):
                raise ValueError("Nonfinite risk input")
            if portfolio.high_watermark <= 0 or portfolio.correlated_open_risk < 0 or estimated_cost_per_unit < 0:
                raise ValueError("Invalid portfolio/cost")
            if (
                proposal.side not in {Side.BUY, Side.SELL}
                or not 0
                <= (now - utc(proposal.signal_timestamp)).total_seconds()
                <= self.limits.maximum_state_age_seconds
            ):
                raise ValueError("Invalid or stale proposal")
            for position in portfolio.positions:
                if type(position.quantity) is not int or position.quantity <= 0 or not position.open_risk.is_finite():
                    raise ValueError("Invalid open position")
            base = RiskEngine(self.risk).evaluate(proposal, portfolio, estimated_cost_per_unit)
            reasons.extend(base.reason_codes)
            budget = base.risk_budget
            quantity = min(base.quantity, self.limits.maximum_quantity)
            quantity = quantity // proposal.instrument.lot_size * proposal.instrument.lot_size
            if quantity <= 0:
                reasons.append("NO_PERMITTED_QUANTITY")
            loss = (abs(proposal.entry_price - proposal.stop_price) + estimated_cost_per_unit) * quantity
            if loss > portfolio.equity * self.risk.maximum_risk_per_trade:
                reasons.append("MAXIMUM_TRADE_RISK")
        except (ValueError, TypeError, ArithmeticError):
            reasons.append("INVALID_PROPOSAL_OR_PORTFOLIO")
        result = {
            "version": VERSION,
            "decision": Decision.REJECT.value if reasons else Decision.APPROVE.value,
            "mode": "PAPER",
            "proposal_id": str(proposal.proposal_id),
            "at": now.isoformat(),
            "quantity": 0 if reasons else quantity,
            "risk_budget": str(budget),
            "estimated_loss": str(loss),
            "reason_codes": sorted(set(reasons)) or ["MANDATORY_RISK_CHECKS_PASSED"],
            "configuration": _encoded(
                {"risk": asdict(self.risk), "limits": asdict(self.limits), "calendar_version": self.calendar.version}
            ),
            "state": _encoded(asdict(state)),
            "portfolio": _encoded(asdict(portfolio)),
            "proposal": {
                "instrument": proposal.instrument.symbol,
                "side": proposal.side.value,
                "entry": str(proposal.entry_price),
                "stop": str(proposal.stop_price),
                "target": str(proposal.target_price),
                "signal_at": proposal.signal_timestamp.isoformat(),
            },
            "estimated_cost_per_unit": str(estimated_cost_per_unit),
        }
        result["fingerprint"] = _hash(result)
        audit.append("deterministic_risk_policy", result)
        return result
