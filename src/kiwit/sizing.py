"""Decimal-only plan calculation from trusted strategy rules; no broker side effects."""

from dataclasses import asdict, dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, localcontext

from .ml.datasets import _hash

VERSION = "position-plan-v1"


@dataclass(frozen=True)
class SizingRules:
    version: str
    stop_method: str = "ATR"
    stop_multiplier: Decimal = Decimal(2)
    stop_fraction: Decimal = Decimal("0.01")
    reward_multiple: Decimal = Decimal(2)
    minimum_net_reward_risk: Decimal = Decimal("1.5")
    risk_fraction: Decimal = Decimal("0.005")
    maximum_risk_fraction: Decimal = Decimal("0.01")
    maximum_quantity: int = 1000

    def __post_init__(self):
        if not self.version or self.stop_method not in {"ATR", "FRACTION"}:
            raise ValueError("Version and supported numeric stop method required")
        for value in (
            self.stop_multiplier,
            self.stop_fraction,
            self.reward_multiple,
            self.minimum_net_reward_risk,
            self.risk_fraction,
            self.maximum_risk_fraction,
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ValueError("Rules require positive finite Decimal values")
        if not self.risk_fraction <= self.maximum_risk_fraction < 1 or self.stop_fraction >= 1:
            raise ValueError("Invalid fraction limits")
        if type(self.maximum_quantity) is not int or self.maximum_quantity <= 0:
            raise ValueError("Maximum quantity must be a positive integer")


@dataclass(frozen=True)
class SizingInput:
    instrument: str
    snapshot_fingerprint: str
    direction: str
    reference_price: Decimal
    equity: Decimal
    available_capital: Decimal
    tick_size: Decimal
    lot_size: int
    atr: Decimal | None = None
    round_trip_cost_per_unit: Decimal = Decimal(0)
    entry_cost_per_unit: Decimal = Decimal(0)


def _serialize(data):
    return {key: str(value) if isinstance(value, Decimal) else value for key, value in data.items()}


def calculate_plan(inputs: SizingInput, rules: SizingRules):
    """Conservative unlevered capital sizing; stop losses may exceed plans if markets gap."""
    result = {
        "version": VERSION,
        "rules": _serialize(asdict(rules)),
        "inputs": _serialize(asdict(inputs)),
        "status": "REJECT",
        "quantity": 0,
        "entry": None,
        "stop": None,
        "target": None,
        "risk_budget": None,
        "planned_loss": None,
        "net_reward_risk": None,
        "execution_allowed": False,
        "reason_codes": [],
    }
    reasons = result["reason_codes"]
    values = (inputs.reference_price, inputs.equity, inputs.available_capital, inputs.tick_size)
    if (
        not inputs.instrument
        or not inputs.snapshot_fingerprint
        or inputs.direction not in {"LONG", "SHORT"}
        or type(inputs.lot_size) is not int
        or inputs.lot_size <= 0
        or any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0 for v in values)
        or any(
            not isinstance(v, Decimal) or not v.is_finite() or v < 0
            for v in (inputs.round_trip_cost_per_unit, inputs.entry_cost_per_unit)
        )
    ):
        reasons.append("INVALID_SIZING_INPUT")
    elif inputs.entry_cost_per_unit > inputs.round_trip_cost_per_unit:
        reasons.append("INCONSISTENT_COSTS")
    elif rules.stop_method == "ATR" and (
        not isinstance(inputs.atr, Decimal) or not inputs.atr.is_finite() or inputs.atr <= 0
    ):
        reasons.append("ATR_UNAVAILABLE")
    else:
        with localcontext(Context(prec=50)):
            try:
                long = inputs.direction == "LONG"
                tick = inputs.tick_size

                def rounded(value, up):
                    return (value / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick

                entry = rounded(inputs.reference_price, long)
                distance = (
                    inputs.atr * rules.stop_multiplier if rules.stop_method == "ATR" else entry * rules.stop_fraction
                )
                stop = rounded(entry - distance if long else entry + distance, not long)
                risk = abs(entry - stop)
                target = rounded(
                    entry + risk * rules.reward_multiple if long else entry - risk * rules.reward_multiple, long
                )
                per_unit_loss = risk + inputs.round_trip_cost_per_unit
                reward = abs(target - entry) - inputs.round_trip_cost_per_unit
                budget = inputs.equity * min(rules.risk_fraction, rules.maximum_risk_fraction)
                result.update(entry=str(entry), stop=str(stop), target=str(target), risk_budget=str(budget))
                if (
                    min(entry, stop, target) <= 0
                    or risk <= 0
                    or not (stop < entry < target if long else target < entry < stop)
                ):
                    reasons.append("INVALID_ROUNDED_PRICE_GEOMETRY")
                else:
                    rr = reward / per_unit_loss
                    result["net_reward_risk"] = str(rr)
                    if rr < rules.minimum_net_reward_risk:
                        reasons.append("INSUFFICIENT_NET_REWARD_RISK")
                    cap = min(
                        budget / per_unit_loss,
                        inputs.available_capital / (entry + inputs.entry_cost_per_unit),
                        Decimal(rules.maximum_quantity),
                    )
                    quantity = int(cap.to_integral_value(rounding=ROUND_FLOOR)) // inputs.lot_size * inputs.lot_size
                    if quantity <= 0:
                        reasons.append("BELOW_MINIMUM_LOT")
                    if not reasons:
                        result.update(
                            status="CALCULATED_REQUIRES_RISK",
                            quantity=quantity,
                            planned_loss=str(per_unit_loss * quantity),
                        )
            except ArithmeticError:
                reasons.append("UNREPRESENTABLE_SIZING_INPUT")
    if not reasons:
        result["reason_codes"] = ["DETERMINISTIC_PLAN_RISK_APPROVAL_REQUIRED"]
    result["fingerprint"] = _hash(result)
    return result
