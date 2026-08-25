from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .domain import Environment


@dataclass(frozen=True)
class RiskConfig:
    risk_per_trade: Decimal
    maximum_risk_per_trade: Decimal
    maximum_open_risk: Decimal
    maximum_correlated_risk: Decimal
    daily_loss_limit_r: Decimal
    weekly_loss_limit_r: Decimal
    drawdown_throttle: Decimal
    drawdown_halt: Decimal
    maximum_positions: int

    def __post_init__(self) -> None:
        fractions = (
            self.risk_per_trade,
            self.maximum_risk_per_trade,
            self.maximum_open_risk,
            self.maximum_correlated_risk,
            self.drawdown_throttle,
            self.drawdown_halt,
        )
        if any(value <= 0 or value >= 1 for value in fractions):
            raise ValueError("risk fractions must be between zero and one")
        if self.risk_per_trade > self.maximum_risk_per_trade:
            raise ValueError("risk_per_trade exceeds maximum")
        if self.drawdown_throttle >= self.drawdown_halt:
            raise ValueError("drawdown throttle must precede halt")
        if self.maximum_positions < 1:
            raise ValueError("maximum_positions must be positive")


@dataclass(frozen=True)
class ExecutionConfig:
    mode: Environment
    maximum_quote_age_seconds: int
    maximum_price_deviation: Decimal
    require_human_approval: bool


@dataclass(frozen=True)
class AppConfig:
    name: str
    environment: Environment
    timezone: str
    currency: str
    live_execution_enabled: bool
    risk: RiskConfig
    execution: ExecutionConfig

    def __post_init__(self) -> None:
        if self.environment == Environment.LIVE and not self.live_execution_enabled:
            raise ValueError("live environment requires explicit live_execution_enabled")
        if self.execution.mode == Environment.LIVE and not self.live_execution_enabled:
            raise ValueError("live execution is disabled")


def load_config(path: str | Path) -> AppConfig:
    with Path(path).open("rb") as stream:
        raw = tomllib.load(stream)
    app = raw["application"]
    risk = raw["risk"]
    execution = raw["execution"]
    return AppConfig(
        name=app["name"],
        environment=Environment(app["environment"]),
        timezone=app["timezone"],
        currency=app["currency"],
        live_execution_enabled=bool(app["live_execution_enabled"]),
        risk=RiskConfig(
            risk_per_trade=Decimal(risk["risk_per_trade"]),
            maximum_risk_per_trade=Decimal(risk["maximum_risk_per_trade"]),
            maximum_open_risk=Decimal(risk["maximum_open_risk"]),
            maximum_correlated_risk=Decimal(risk["maximum_correlated_risk"]),
            daily_loss_limit_r=Decimal(risk["daily_loss_limit_r"]),
            weekly_loss_limit_r=Decimal(risk["weekly_loss_limit_r"]),
            drawdown_throttle=Decimal(risk["drawdown_throttle"]),
            drawdown_halt=Decimal(risk["drawdown_halt"]),
            maximum_positions=int(risk["maximum_positions"]),
        ),
        execution=ExecutionConfig(
            mode=Environment(execution["mode"]),
            maximum_quote_age_seconds=int(execution["maximum_quote_age_seconds"]),
            maximum_price_deviation=Decimal(execution["maximum_price_deviation"]),
            require_human_approval=bool(execution["require_human_approval"]),
        ),
    )

