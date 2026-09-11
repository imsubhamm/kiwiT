from datetime import datetime, timedelta
from decimal import Decimal as D
from uuid import UUID

import pytest

from kiwit.domain import Decision, Instrument, Quote, RiskDecision, Side, TradeProposal
from kiwit.marketdata.canonical import IST, Session, TradingCalendar
from kiwit.paper_simulator import FillRules, PaperSimulator

AT = datetime(2026, 9, 11, 10, tzinfo=IST)
INSTRUMENT = Instrument("TEST")
CAL = TradingCalendar("test", (Session(AT.date()),))


def engine(path):
    return PaperSimulator(
        path,
        initial_capital=D(10000),
        calendar=CAL,
        rules=FillRules(slippage_fraction=D(".001"), fee_fraction=D(".001")),
    )


def submit(sim, side=Side.BUY):
    p = TradeProposal(
        "test",
        "v1",
        INSTRUMENT,
        side,
        AT,
        D(100),
        D(98) if side == Side.BUY else D(102),
        D(104) if side == Side.BUY else D(96),
        proposal_id=UUID(int=1),
    )
    r = RiskDecision(Decision.APPROVE, p.proposal_id, 10, D(50), D(20), ())
    return sim.submit(p, r, at=AT)


def quote(bid, ask, at):
    return Quote(INSTRUMENT, at, D(bid), D(ask), D(bid))


def test_spread_fees_target_and_restart(tmp_path):
    sim = engine(tmp_path / "sim.db")
    submit(sim)
    tick = quote("100", "100.1", AT)
    first = sim.on_quote("q1", tick, received_at=AT)
    assert first[0]["status"] == "OPEN"
    assert D(first[0]["entry"]) > tick.ask
    sim = engine(tmp_path / "sim.db")
    assert sim.on_quote("q1", tick, received_at=AT) == first
    at = AT + timedelta(minutes=1)
    result = sim.on_quote("q2", quote("104", "104.1", at), received_at=at)[0]
    assert result["status"] == "CLOSED" and result["reason"] == "TARGET"
    assert len(result["fills"]) == 2
    portfolio = sim.portfolio()
    assert D(portfolio["equity"]) == D(10000) + D(portfolio["realized_pnl"])
    assert portfolio["unrealized_pnl"] == "0"


def test_stop_gap_short_and_session_exit(tmp_path):
    sim = engine(tmp_path / "sim.db")
    submit(sim, Side.SELL)
    sim.on_quote("q1", quote("100", "100.1", AT), received_at=AT)
    at = AT + timedelta(minutes=1)
    result = sim.on_quote("q2", quote("104", "104.1", at), received_at=at)[0]
    assert result["reason"] == "STOP"
    assert D(result["fills"][-1]["price"]) > D(102)  # Gap uses executable quote, not the stop level.
    assert D(result["realized_pnl"]) < 0
    other = engine(tmp_path / "other.db")
    submit(other)
    other.on_quote("q1", quote("100", "100.1", AT), received_at=AT)
    close = AT.replace(hour=15, minute=30)
    other.end_session(at=close)
    assert next(iter(engine(tmp_path / "other.db").portfolio()["orders"].values()))["status"] == "EXIT_PENDING"
    assert other.on_quote("exit", quote("101", "101.1", close), received_at=close)[0]["reason"] == "SESSION_EXIT"


def test_future_stale_duplicates_and_cancel(tmp_path):
    sim = engine(tmp_path / "sim.db")
    order = submit(sim)
    future = AT + timedelta(seconds=10)
    with pytest.raises(ValueError):
        sim.on_quote("future", quote("100", "101", future), received_at=AT)
    with pytest.raises(ValueError):
        sim.on_quote("stale", quote("100", "101", AT), received_at=future)
    sim.cancel(order["id"], at=AT)
    assert sim.on_quote("q1", quote("100", "101", AT), received_at=AT) == []
    with pytest.raises(ValueError):
        sim.on_quote("q1", quote("101", "102", AT), received_at=AT)


def test_replay_and_pending_expiry(tmp_path):
    def run(path):
        sim = engine(path)
        submit(sim)
        sim.end_session(at=AT.replace(hour=15, minute=30))
        return sim.portfolio()

    assert run(tmp_path / "a") == run(tmp_path / "b")
    assert next(iter(run(tmp_path / "c")["orders"].values()))["status"] == "CANCELLED"


def test_fill_budget_rejection_and_configuration_binding(tmp_path):
    sim = engine(tmp_path / "sim.db")
    p = TradeProposal("test", "v1", INSTRUMENT, Side.BUY, AT, D(100), D(98), D(104))
    risk = RiskDecision(Decision.REJECT, p.proposal_id, 10, D(1), D(1), ())
    with pytest.raises(ValueError):
        sim.submit(p, risk, at=AT)
    risk = RiskDecision(Decision.APPROVE, p.proposal_id, 10, D(1), D(1), ())
    sim.submit(p, risk, at=AT)
    result = sim.on_quote("q", quote('100', '100.1', AT), received_at=AT)[0]
    assert result["status"] == "REJECTED"
    assert sim.portfolio()["cash"] == '10000'
    with pytest.raises(ValueError):
        PaperSimulator(tmp_path / "sim.db", initial_capital=D(10000), calendar=CAL)
