import asyncio
from decimal import Decimal as D

from test_decision_journal import AT, CALENDAR, INSTRUMENT, MINUTE, candle, setup
from test_llm_gateway import Audit, Provider, reply

from kiwit.config import load_config
from kiwit.domain import Instrument, Quote
from kiwit.llm.critic import TradeCritic
from kiwit.llm.gateway import LLMGateway
from kiwit.ml.candidates import generate_candidates
from kiwit.ml.datasets import _hash
from kiwit.paper_simulator import PaperSimulator
from kiwit.risk_policy import DeterministicRiskPolicy, HardLimits
from kiwit.sizing import SizingInput, SizingRules, calculate_plan
from kiwit.unified_backtest import PaperDecisionCore, run_backtest

EXEC = Instrument(INSTRUMENT.symbol, series=INSTRUMENT.series)
RULES = SizingRules("fixture", risk_fraction=D(".0025"), maximum_risk_fraction=D(".005"))


class Registry:
    def score(self, kind, snapshot):
        pred = (
            {"regime": "UPTREND", "scores": {"UPTREND": 0.9}}
            if kind == "regime"
            else {
                "direction": "LONG" if kind == "trend" else "NONE",
                {
                    "trend": "opportunity_probability",
                    "breakout": "continuation_probability",
                    "mean_reversion": "reversion_probability",
                }[kind]: 0.8,
            }
        )
        return {
            "kind": kind,
            "status": "SCORED",
            "model_fingerprint": kind,
            "model_version": "fixture",
            "training_data_end": "2026-09-01T00:00:00+00:00",
            "snapshot_fingerprint": _hash(snapshot.to_json_dict()),
            "feature_version": snapshot.version,
            "prediction": {
                **pred,
                "model_fingerprint": kind,
                "model_version": "fixture",
                "feature_version": snapshot.version,
            },
        }


def build(root):
    journal, history, _market, snap = setup(root)
    sim = PaperSimulator(root / "sim.db", initial_capital=D(100000), calendar=CALENDAR)
    policy = DeterministicRiskPolicy(
        load_config("config/kiwit.toml").risk, HardLimits(emergency_disabled=False), CALENDAR
    )
    return PaperDecisionCore(
        history=history,
        calendar=CALENDAR,
        instrument=INSTRUMENT,
        execution_instrument=EXEC,
        registry=Registry(),
        journal=journal,
        simulator=sim,
        risk_policy=policy,
        sizing_rules=RULES,
        audit=Audit(),
    ), snap


def tape_for(core, snap):
    scores = {k: core.registry.score(k, snap) for k in ("regime", "trend", "breakout", "mean_reversion")}
    candidate = generate_candidates(snap, scores)["candidates"][0]
    quote = Quote(EXEC, AT, D(140), D("140.05"), D(140))
    rules = core.simulator.rules
    cost = quote.ask * (rules.slippage_fraction + rules.fee_fraction) * 4 + EXEC.tick_size * 2
    plan = calculate_plan(
        SizingInput(
            INSTRUMENT.key,
            _hash(snap.to_json_dict()),
            "LONG",
            quote.ask,
            D(100000),
            D(100000),
            EXEC.tick_size,
            EXEC.lot_size,
            D(str(snap.values["atr_14"])),
            cost,
            quote.ask * rules.fee_fraction,
        ),
        RULES,
    )
    data = {
        "instrument": INSTRUMENT.symbol,
        "as_of": AT.isoformat(),
        "snapshot_fingerprint": _hash(snap.to_json_dict()),
        "quant": {
            "regime": "UPTREND",
            "trend_probability": 0.8,
            "breakout_probability": 0.8,
            "reversion_probability": 0.8,
            "volatility": 0.001,
            "session_fraction": 0.2,
        },
        "evidence": [
            {
                "source_id": "s1",
                "instrument": "MARKET",
                "published_at": AT.isoformat(),
                "available_at": AT.isoformat(),
                "category": "NEWS",
                "text": "Fixture only",
            }
        ],
        "features": {"rsi": 55.0, "atr": 3.0, "ema_distance": 0.001},
        "historical_similarity": "Fixture only",
        "proposal": {
            "candidate_id": candidate["candidate_id"],
            "direction": "LONG",
            "entry": plan["entry"],
            "stop": plan["stop"],
            "target": plan["target"],
            "quantity": plan["quantity"],
            "confidence": candidate["confidence"],
            "thesis": candidate["thesis"],
        },
    }
    import json

    data["context"] = json.loads(reply().content)
    critic = TradeCritic(LLMGateway({"deepseek": Provider([reply(True)])}, Audit()))
    review = asyncio.run(critic.critique(data))
    checks = {
        "as_of": AT.isoformat(),
        "snapshot_fingerprint": _hash(snap.to_json_dict()),
        "spread_fraction": 0.001,
        "depth": 1000,
        "context_confidence": 0.8,
        "critic": "PASS",
        "critic_review": review,
    }
    return {snap.as_of.isoformat(): {"quote": quote, "checks": checks}}, plan


def test_unified_submission_fill_exit_and_reproducible_replay(tmp_path):
    core, snap = build(tmp_path / "a")
    tape, plan = tape_for(core, snap)
    for i in (40, 41):
        c = candle(i)
        core.history.record([c], available_at=c.closed_at, raw_payload=str(i).encode(), source="fixture")
    tape[(snap.as_of + MINUTE).isoformat()] = {"quote": Quote(EXEC, AT + MINUTE, D(140), D("140.05"), D(140))}
    target = D(plan["target"]) + D(1)
    tape[(snap.as_of + 2 * MINUTE).isoformat()] = {
        "quote": Quote(EXEC, AT + 2 * MINUTE, target, target + D(".05"), target)
    }
    report = run_backtest(core, start=AT - MINUTE, end=AT + 2 * MINUTE, tape=tape)
    assert report["decisions"][0]["status"] == "SUBMITTED"
    assert report["metrics"]["trades"] == 1
    assert report["metrics"]["wins"] == 1
    assert report["by_strategy"]["trend"]["trades"] == 1
    second, _ = build(tmp_path / "b")
    for i in (40, 41):
        c = candle(i)
        second.history.record([c], available_at=c.closed_at, raw_payload=str(i).encode(), source="fixture")
    again = run_backtest(second, start=AT - MINUTE, end=AT + 2 * MINUTE, tape=tape)
    assert again["fingerprint"] == report["fingerprint"]


def test_future_training_blocks_and_missing_quote_never_fills(tmp_path):
    core, snap = build(tmp_path / "a")
    tape, _ = tape_for(core, snap)
    old = core.registry.score

    def future(kind, snapshot):
        return {**old(kind, snapshot), "training_data_end": "2030-01-01T00:00:00+00:00"}

    core.registry.score = future
    report = run_backtest(core, start=AT - MINUTE, end=AT, tape=tape)
    assert not report["portfolio"]["orders"]


def test_paper_process_matches_replay_and_future_quote_rejected(tmp_path):
    import pytest

    core, snap = build(tmp_path / "paper")
    tape, _ = tape_for(core, snap)
    event = tape[snap.as_of.isoformat()]
    core.process(at=snap.as_of, **event)
    replay, _ = build(tmp_path / "replay")
    result = run_backtest(replay, start=AT - MINUTE, end=AT, tape=tape)
    assert core.report()["fingerprint"] == result["fingerprint"]
    fresh, _ = build(tmp_path / "future")
    future_quote = Quote(EXEC, AT + MINUTE, D(140), D("140.05"), D(140))
    with pytest.raises(ValueError):
        fresh.process(at=AT, quote=future_quote, checks=event["checks"])
    assert not fresh.simulator.portfolio()["orders"]
