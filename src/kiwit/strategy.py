from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from .domain import Instrument, Side, TradeProposal


@dataclass(frozen=True)
class StrategyMetadata:
    strategy_id: str
    version: str
    status: str
    description: str


@dataclass(frozen=True)
class StrategyContext:
    as_of: datetime
    instrument: Instrument
    features: Mapping[str, Decimal | int | bool]


class Strategy(Protocol):
    metadata: StrategyMetadata

    def evaluate(self, context: StrategyContext) -> TradeProposal | None: ...


class StrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[tuple[str, str], Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        key = (strategy.metadata.strategy_id, strategy.metadata.version)
        if key in self._strategies:
            raise ValueError(f"strategy already registered: {key}")
        self._strategies[key] = strategy

    def get(self, strategy_id: str, version: str) -> Strategy:
        try:
            return self._strategies[(strategy_id, version)]
        except KeyError as exc:
            raise KeyError(f"unknown strategy: {strategy_id}@{version}") from exc

    def get_for_paper(self, strategy_id: str, version: str) -> Strategy:
        strategy = self.get(strategy_id, version)
        if strategy.metadata.status not in {"paper", "approved"}:
            raise PermissionError(f"strategy is not approved for paper trading: {strategy_id}@{version}")
        return strategy


class TrendPullbackResearchStrategy:
    metadata = StrategyMetadata(
        strategy_id="trend_pullback",
        version="0.0.1-rejected",
        status="rejected",
        description="Archived Strategy A v0; retained for reproducibility and cannot emit proposals.",
    )

    def evaluate(self, context: StrategyContext) -> TradeProposal | None:
        return None


class DonchianBaselineStrategy:
    metadata = StrategyMetadata(
        strategy_id="donchian_baseline",
        version="0.1.0-research",
        status="research",
        description="Long-only research baseline; breakout with a lower-channel invalidation stop.",
    )

    def evaluate(self, context: StrategyContext) -> TradeProposal | None:
        f = context.features
        required = {"close", "prior_high_50", "prior_low_20", "regime_positive"}
        if not required.issubset(f):
            return None
        if not bool(f["regime_positive"]) or Decimal(f["close"]) <= Decimal(f["prior_high_50"]):
            return None
        entry = Decimal(f["close"])
        stop = Decimal(f["prior_low_20"])
        if stop >= entry:
            return None
        return TradeProposal(
            strategy_id=self.metadata.strategy_id,
            strategy_version=self.metadata.version,
            instrument=context.instrument,
            side=Side.BUY,
            signal_timestamp=context.as_of,
            entry_price=entry,
            stop_price=stop,
            target_price=None,
            rationale={"rule": "close_above_prior_50_day_high"},
        )
