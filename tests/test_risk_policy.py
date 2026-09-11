from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D

import pytest

from kiwit.config import load_config
from kiwit.domain import Instrument, PortfolioSnapshot, Position, Side, TradeProposal
from kiwit.marketdata.canonical import IST, Session, TradingCalendar
from kiwit.risk_policy import DeterministicRiskPolicy, HardLimits, RiskState

AT = datetime(2026, 9, 11, 10, tzinfo=IST)


class Audit:
    def __init__(self):
        self.records = []

    def append(self, kind, value):
        self.records.append(value)


def evaluate(*, limits=None, state=None, portfolio=None, now=AT, audit=None):
    policy = DeterministicRiskPolicy(
        load_config("config/kiwit.toml").risk,
        limits or HardLimits(emergency_disabled=False, maximum_quantity=100),
        TradingCalendar("test", (Session(AT.date()),)),
    )
    proposal = TradeProposal("test", "v1", Instrument("TEST"), Side.BUY, now, D(100), D(99), D(102))
    return policy.evaluate(
        proposal,
        portfolio or PortfolioSnapshot(D(100000), D(100000)),
        D(".1"),
        state=state or RiskState(now, now.date().isoformat(), 0, 0),
        now=now,
        audit=audit or Audit(),
    )


def test_size_cap_budget_and_audit():
    audit = Audit()
    result = evaluate(audit=audit)
    assert result["decision"] == "approve"
    assert result["quantity"] == 100
    assert D(result["estimated_loss"]) <= D(result["risk_budget"])
    assert audit.records == [result]


@pytest.mark.parametrize(
    "limits,reason",
    [
        (HardLimits(), "EMERGENCY_DISABLED"),
        (HardLimits(emergency_disabled=False, maximum_trades_per_day=1), "MAXIMUM_DAILY_TRADES"),
    ],
)
def test_limit_blocks(limits, reason):
    result = evaluate(limits=limits, state=RiskState(AT, AT.date().isoformat(), 1, 0))
    assert result["decision"] == "reject" and result["quantity"] == 0
    assert reason in result["reason_codes"]


def test_loss_streak_cooldown_and_boundary():
    state = RiskState(AT, AT.date().isoformat(), 0, 3, AT - timedelta(seconds=1799))
    assert "CONSECUTIVE_LOSS_COOLDOWN" in evaluate(state=state)["reason_codes"]
    assert evaluate(state=replace(state, last_loss_at=AT - timedelta(seconds=1800)))["decision"] == "approve"


@pytest.mark.parametrize("now", [AT.replace(hour=9, minute=0), AT.replace(hour=15, minute=15), AT + timedelta(days=1)])
def test_hours_cutoff_and_calendar(now):
    assert evaluate(now=now)["decision"] == "reject"


def test_daily_drawdown_positions_and_stale():
    base = PortfolioSnapshot(D(100000), D(100000))
    assert "DAILY_LOSS_LIMIT" in evaluate(portfolio=replace(base, realized_daily_pnl=D(-5000)))["reason_codes"]
    assert "DRAWDOWN_HALT" in evaluate(portfolio=replace(base, equity=D(50000)))["reason_codes"]
    position = Position(Instrument("TEST"), Side.BUY, 1, D(100), D(99), "test", AT)
    maximum = load_config("config/kiwit.toml").risk.maximum_positions
    assert "MAXIMUM_POSITIONS" in evaluate(portfolio=replace(base, positions=(position,) * maximum))["reason_codes"]
    assert (
        "STALE_OR_FUTURE_RISK_STATE"
        in evaluate(state=RiskState(AT - timedelta(seconds=31), AT.date().isoformat(), 0, 0))["reason_codes"]
    )
    assert evaluate(portfolio=replace(base, correlated_open_risk=D("NaN")))["decision"] == "reject"


def test_audit_failure_and_immutable_limits():
    class Broken:
        def append(self, *args):
            raise OSError("disk full")

    with pytest.raises(OSError):
        evaluate(audit=Broken())
    with pytest.raises(AttributeError):
        HardLimits().maximum_quantity = 999999
