from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Mapping
from uuid import UUID, uuid4


ZERO = Decimal("0")


class Environment(StrEnum):
    RESEARCH = "research"
    PAPER = "paper"
    LIVE = "live"


class Side(StrEnum):
    BUY = "buy"
    SELL = "sell"


class Decision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    NO_TRADE = "no_trade"


class WorkflowStatus(StrEnum):
    RECEIVED = "received"
    DATA_VALIDATED = "data_validated"
    SIGNAL_GENERATED = "signal_generated"
    RISK_APPROVED = "risk_approved"
    AWAITING_HUMAN = "awaiting_human"
    EXECUTED = "executed"
    REJECTED = "rejected"
    HALTED = "halted"


@dataclass(frozen=True)
class Instrument:
    symbol: str
    exchange: str = "NSE"
    series: str = "EQ"
    lot_size: int = 1
    tick_size: Decimal = Decimal("0.05")

    def __post_init__(self) -> None:
        if not self.symbol or self.lot_size < 1 or self.tick_size <= ZERO:
            raise ValueError("invalid instrument")


@dataclass(frozen=True)
class Quote:
    instrument: Instrument
    timestamp: datetime
    bid: Decimal
    ask: Decimal
    last: Decimal

    def __post_init__(self) -> None:
        if min(self.bid, self.ask, self.last) <= ZERO or self.ask < self.bid:
            raise ValueError("invalid quote")


@dataclass(frozen=True)
class TradeProposal:
    strategy_id: str
    strategy_version: str
    instrument: Instrument
    side: Side
    signal_timestamp: datetime
    entry_price: Decimal
    stop_price: Decimal
    target_price: Decimal | None
    rationale: Mapping[str, str | int | float | bool] = field(default_factory=dict)
    proposal_id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if self.entry_price <= ZERO or self.stop_price <= ZERO:
            raise ValueError("entry and stop must be positive")
        if self.side == Side.BUY and self.stop_price >= self.entry_price:
            raise ValueError("buy stop must be below entry")
        if self.side == Side.SELL and self.stop_price <= self.entry_price:
            raise ValueError("sell stop must be above entry")


@dataclass(frozen=True)
class Position:
    instrument: Instrument
    side: Side
    quantity: int
    entry_price: Decimal
    stop_price: Decimal
    strategy_id: str
    opened_at: datetime

    @property
    def open_risk(self) -> Decimal:
        return abs(self.entry_price - self.stop_price) * self.quantity


@dataclass(frozen=True)
class PortfolioSnapshot:
    equity: Decimal
    high_watermark: Decimal
    realized_daily_pnl: Decimal = ZERO
    realized_weekly_pnl: Decimal = ZERO
    positions: tuple[Position, ...] = ()
    correlated_open_risk: Decimal = ZERO

    @property
    def drawdown_fraction(self) -> Decimal:
        if self.high_watermark <= ZERO:
            return ZERO
        return max(ZERO, (self.high_watermark - self.equity) / self.high_watermark)


@dataclass(frozen=True)
class RiskDecision:
    decision: Decision
    proposal_id: UUID
    quantity: int
    risk_budget: Decimal
    estimated_loss: Decimal
    reason_codes: tuple[str, ...]

