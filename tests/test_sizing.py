from dataclasses import replace
from decimal import Decimal as D
from decimal import localcontext

import pytest

from kiwit.sizing import SizingInput, SizingRules, calculate_plan


def data():
    return SizingInput("TEST", "snapshot", "LONG", D("100.03"), D(100000), D(100000), D(".05"), 10, D(1))


def test_determinism_rounding_and_risk_cap():
    rules = SizingRules("test")
    plan = calculate_plan(data(), rules)
    assert plan == calculate_plan(data(), rules)
    assert plan["entry"] == "100.05"
    assert plan["quantity"] % 10 == 0
    assert D(plan["planned_loss"]) <= D(plan["risk_budget"])
    for field in ("entry", "stop", "target"):
        assert D(plan[field]) % D(".05") == 0
    with localcontext() as context:
        context.prec = 8
        assert calculate_plan(data(), rules) == plan


def test_short_geometry_capital_and_quantity_cap():
    plan = calculate_plan(replace(data(), direction="SHORT", available_capital=D(1050)), SizingRules("test"))
    assert D(plan["target"]) < D(plan["entry"]) < D(plan["stop"])
    assert plan["quantity"] == 10
    assert D(plan["entry"]) * plan["quantity"] <= 1050
    plan = calculate_plan(data(), SizingRules("test", maximum_quantity=25))
    assert plan["quantity"] == 20


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"atr": None}, "ATR_UNAVAILABLE"),
        ({"equity": D("NaN")}, "INVALID_SIZING_INPUT"),
        ({"lot_size": 0}, "INVALID_SIZING_INPUT"),
        ({"available_capital": D(1)}, "BELOW_MINIMUM_LOT"),
        ({"atr": D(100)}, "INVALID_ROUNDED_PRICE_GEOMETRY"),
        ({"round_trip_cost_per_unit": D(2)}, "INSUFFICIENT_NET_REWARD_RISK"),
    ],
)
def test_rejections(changes, reason):
    plan = calculate_plan(replace(data(), **changes), SizingRules("test"))
    assert plan["status"] == "REJECT" and plan["quantity"] == 0
    assert reason in plan["reason_codes"]


def test_fraction_stop_and_no_authoritative_quantity_input():
    plan = calculate_plan(replace(data(), atr=None), SizingRules("test", stop_method="FRACTION"))
    assert plan["status"] == "CALCULATED_REQUIRES_RISK"
    assert plan["execution_allowed"] is False
    with pytest.raises(TypeError):
        SizingInput(**{**data().__dict__, "quantity": 999})
    with pytest.raises(ValueError):
        SizingRules("test", risk_fraction=D(".5"))
