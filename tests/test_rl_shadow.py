from copy import deepcopy
from decimal import Decimal

import pytest

from kiwit.rl.shadow import ShadowEnvironment, compare_shadow


def tape():
    return [
        {
            "at": f"2026-09-01T04:0{i}:00+00:00",
            "available_at": f"2026-09-01T04:0{i}:00+00:00",
            "bid": "100",
            "ask": "101",
            "features": {"score": i / 10},
            "allowed_entries": ["LONG", "SHORT"],
        }
        for i in range(5)
    ]


def test_observation_is_current_and_cannot_mutate_tape():
    env = ShadowEnvironment(tape())
    obs = env.reset()
    assert obs["features"] == {"score": 0}
    obs["features"]["score"] = 99
    assert env.observation()["features"]["score"] == 0
    with pytest.raises(ValueError):
        env.reset()
    obs, _, _, _ = env.step("NO_TRADE")
    assert obs["features"]["score"] == 0.1


def test_costs_settlement_reward_identity_and_invalid_actions():
    env = ShadowEnvironment(tape())
    env.reset()
    env.step("LONG")
    env.step("SHORT")  # reversal is forbidden
    while not env.done:
        env.step("NO_TRADE")
    report = env.report()
    assert Decimal(report["net_pnl"]) < 0
    assert report["invalid_actions"] == 1
    assert env.position == 0
    expected = env.net / env.capital - env.max_drawdown - Decimal(".0001")
    assert abs(env.reward_total - expected) < Decimal("1e-24")


def test_future_data_and_overlap_rejected():
    rows = tape()
    rows[0]["available_at"] = rows[1]["at"]
    with pytest.raises(ValueError):
        ShadowEnvironment(rows)
    with pytest.raises(ValueError):
        compare_shadow(
            tape(),
            dict.fromkeys(["candidate", "champion", "no_trade"], lambda _: "NO_TRADE"),
            training_end="2026-09-02T00:00:00+00:00",
            evaluation_start="2026-09-01T00:00:00+00:00",
        )


def test_comparison_is_reproducible_and_no_trade_zero():
    policies = {
        "candidate": lambda obs: "LONG" if obs["position"] == 0 else "NO_TRADE",
        "champion": lambda _: "NO_TRADE",
        "no_trade": None,
    }
    kwargs = {"training_end": "2026-08-31T00:00:00+00:00", "evaluation_start": "2026-09-01T00:00:00+00:00"}
    first = compare_shadow(tape(), policies, **kwargs)
    assert first == compare_shadow(deepcopy(tape()), policies, **kwargs)
    assert first["results"]["no_trade"]["net_pnl"] == "0"
    assert first["promotion"] == "NOT_AUTHORIZED"
