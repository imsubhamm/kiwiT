from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from .domain import Quote, RiskDecision, Side, TradeProposal


@dataclass(frozen=True)
class PaperFill:
    fill_id: UUID
    proposal_id: UUID
    timestamp: datetime
    side: Side
    quantity: int
    price: Decimal


class FillAccumulator:
    """Idempotently reconcile broker partial fills without exceeding order quantity."""

    def __init__(self, ordered_quantity: int) -> None:
        if ordered_quantity <= 0:
            raise ValueError("ordered quantity must be positive")
        self.ordered_quantity = ordered_quantity
        self._fills: dict[str, int] = {}

    def record(self, broker_fill_id: str, quantity: int) -> dict[str, int | str]:
        if not broker_fill_id or quantity <= 0:
            raise ValueError("valid fill ID and positive quantity required")
        previous = self._fills.get(broker_fill_id)
        if previous is not None:
            if previous != quantity:
                raise ValueError("duplicate fill ID has conflicting quantity")
        elif sum(self._fills.values()) + quantity > self.ordered_quantity:
            raise ValueError("fills exceed ordered quantity")
        else:
            self._fills[broker_fill_id] = quantity
        filled = sum(self._fills.values())
        return {"filled_quantity": filled, "remaining_quantity": self.ordered_quantity - filled,
                "status": "filled" if filled == self.ordered_quantity else "partially_filled"}


class PaperBroker:
    def __init__(self, slippage_bps: Decimal = Decimal(5)) -> None:
        self.slippage_bps = slippage_bps
        self._filled_proposals: set[UUID] = set()
        self._fills_by_proposal: dict[UUID, PaperFill] = {}

    def fill_for(self, proposal_id: UUID) -> PaperFill | None:
        return self._fills_by_proposal.get(proposal_id)

    def execute(self, proposal: TradeProposal, risk: RiskDecision, quote: Quote) -> PaperFill:
        if risk.proposal_id != proposal.proposal_id or risk.quantity <= 0:
            raise ValueError("proposal lacks an approved positive quantity")
        if proposal.proposal_id in self._filled_proposals:
            raise ValueError("duplicate proposal execution")
        if quote.instrument != proposal.instrument:
            raise ValueError("quote instrument mismatch")
        multiplier = Decimal(1) + self.slippage_bps / Decimal(10000)
        price = quote.ask * multiplier if proposal.side == Side.BUY else quote.bid / multiplier
        fill = PaperFill(uuid4(), proposal.proposal_id, datetime.now(UTC), proposal.side, risk.quantity, price)
        self._filled_proposals.add(proposal.proposal_id)
        self._fills_by_proposal[proposal.proposal_id] = fill
        return fill
