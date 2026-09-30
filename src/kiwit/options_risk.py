"""Deterministic paper fill and sizing rules, shared by plans and execution."""

from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D

BROKER_COST_VERSION = "groww-nse-equity-options-2026-04-01"
BROKERAGE_PER_ORDER = D(20)
EXCHANGE_RATE = D(".0003503")
IPFT_RATE = D(".000005")
SEBI_RATE = D(".000001")
STAMP_BUY_RATE = D(".00003")
STT_SELL_RATE = D(".0015")
GST_RATE = D(".18")
FIXED_ORDER_COST = BROKERAGE_PER_ORDER * (1 + GST_RATE)
BUY_VARIABLE_RATE = EXCHANGE_RATE + IPFT_RATE + SEBI_RATE + STAMP_BUY_RATE + GST_RATE * (
    EXCHANGE_RATE + IPFT_RATE + SEBI_RATE
)
SELL_VARIABLE_RATE = EXCHANGE_RATE + IPFT_RATE + SEBI_RATE + STT_SELL_RATE + GST_RATE * (
    EXCHANGE_RATE + IPFT_RATE + SEBI_RATE
)
ALLOCATION_FRACTION = D(".25")
PLANNED_RISK_FRACTION = D(".01")


def trade_limits(state):
    return (D(state.get("trade_stop_pct", state["loss_pct"])),
            D(state.get("trade_target_pct", state["profit_pct"])))


def session_limit_reached(state, pnl):
    amount = D(state["amount"])
    return (pnl <= -amount * D(state["loss_pct"]) / 100 or
            (state.get("session_profit_cap_enabled", True) and
             pnl >= amount * D(state["profit_pct"]) / 100))


def cost_breakdown(notional, side="unknown"):
    """Current Groww/NSE option-premium schedule; contract notes remain authoritative."""
    value = D(notional)
    if side not in {"buy", "sell"}:
        # Unknown legacy callers get the more conservative sell side.
        side = "sell"
    exchange = value * EXCHANGE_RATE
    ipft = value * IPFT_RATE
    sebi = value * SEBI_RATE
    gst = (BROKERAGE_PER_ORDER + exchange + ipft + sebi) * GST_RATE
    stamp = value * STAMP_BUY_RATE if side == "buy" else D(0)
    stt = value * STT_SELL_RATE if side == "sell" else D(0)
    total = BROKERAGE_PER_ORDER + exchange + ipft + sebi + gst + stamp + stt
    return {"version": BROKER_COST_VERSION, "side": side, "brokerage": BROKERAGE_PER_ORDER,
            "exchange": exchange, "ipft": ipft, "sebi": sebi, "gst": gst, "stamp": stamp, "stt": stt,
            "total": total,
            "sources": ["https://groww.in/pricing/futures-and-options",
                        "https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax"]}


def fees(notional, side="unknown"):
    return cost_breakdown(notional, side)["total"]


def fill_price(quote, contract, buy):
    tick = D(contract["tick"])
    value = D(quote["ask" if buy else "bid"]) * (D("1.001") if buy else D(".999"))
    return (value / tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR) * tick


def exit_levels(state, profile, fill, quantity, contract):
    """Return immutable cost-aware levels for one experimental playbook."""
    price, units, tick = D(fill), D(quantity), D(contract["tick"])
    stop_pct = min(trade_limits(state)[0], D(str(profile["stop_pct"])))
    target_pct = min(trade_limits(state)[1], D(str(profile["target_pct"])))
    reward_r = D(str(profile["reward_r"]))
    hold = int(profile["max_hold_minutes"])
    if not (price > 0 and units > 0 and tick > 0 and D(0) < stop_pct < D(100)
            and target_pct > 0 and reward_r >= 1 and 1 <= hold <= 120):
        raise ValueError("Invalid playbook exit profile")

    stop = ((price * (1 - stop_pct / 100)) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    entry_total = price * units + fees(price * units, "buy")
    stop_proceeds = stop * units - fees(stop * units, "sell")
    net_risk = entry_total - stop_proceeds
    if net_risk <= 0:
        raise ValueError("Playbook stop does not define positive net risk")

    target_floor = ((price * (1 + target_pct / 100)) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    desired_net_reward = net_risk * reward_r
    required_proceeds = entry_total + desired_net_reward
    required_target = required_proceeds + FIXED_ORDER_COST
    required_target /= units * (1 - SELL_VARIABLE_RATE)
    target = max(target_floor,
                 (required_target / tick).to_integral_value(rounding=ROUND_CEILING) * tick)
    for _ in range(3):
        target_proceeds = target * units - fees(target * units, "sell")
        net_reward = target_proceeds - entry_total
        if net_reward >= desired_net_reward and net_reward > 0:
            round_trip_cost = fees(price * units, "buy") + fees(target * units, "sell")
            gross_reward = (target - price) * units
            cost_share = round_trip_cost / gross_reward
            maximum_cost_share = D(str(profile["max_cost_share"]))
            if cost_share > maximum_cost_share:
                raise ValueError("Estimated costs consume too much of the planned gross reward")
            return {
                "version": str(profile["version"]), "stop": stop, "target": target,
                "stop_pct": stop_pct, "target_floor_pct": target_pct, "reward_r": reward_r,
                "max_hold_minutes": hold,
                "estimated_round_trip_cost": round_trip_cost, "cost_share_of_gross_reward": cost_share,
                "maximum_cost_share": maximum_cost_share,
                "net_risk": net_risk, "net_reward_at_target": net_reward,
                "net_reward_r": net_reward / net_risk,
            }
        target += tick
    raise ValueError("Cost-aware target exceeds bounded search")


def quantity_for(state, contract, quote):
    fill = fill_price(quote, contract, True)
    amount, cash, loss = D(state["amount"]), D(state["cash"]), D(state["loss_pct"]) / 100
    allocation = min(amount * ALLOCATION_FRACTION, cash)
    risk_budget = min(
        amount * PLANNED_RISK_FRACTION,
        max(D(0), amount * loss + D(state.get("realized_pnl", "0")),),
    )
    units = min(
        int(max(D(0), allocation - FIXED_ORDER_COST) / (fill * (1 + BUY_VARIABLE_RATE))),
        int(max(D(0), risk_budget - 2 * FIXED_ORDER_COST) /
            (fill * (D(state.get("trade_stop_pct", state["loss_pct"])) / 100 + BUY_VARIABLE_RATE + SELL_VARIABLE_RATE))),
        quote["ask_size"],
        quote["bid_size"],
        contract["freeze"] - 1,
    )
    return max(0, units // contract["lot"] * contract["lot"])


def _budget_units(budget, per_unit, fixed):
    if per_unit <= 0:
        return 0
    return int(max(D(0), budget - fixed) / per_unit)


def sizing_diagnostics(state, contract, quote):
    """Explain whole-lot sizing without changing quantity_for."""
    fill = fill_price(quote, contract, True)
    amount, cash = D(state["amount"]), D(state["cash"])
    stop_pct = trade_limits(state)[0] / 100
    daily_fraction = D(state["loss_pct"]) / 100
    realized = D(state.get("realized_pnl", "0"))
    entry_unit = fill * (1 + BUY_VARIABLE_RATE)
    risk_unit = fill * (stop_pct + BUY_VARIABLE_RATE + SELL_VARIABLE_RATE)
    notional = fill * contract["lot"]
    planned_risk = notional * (stop_pct + BUY_VARIABLE_RATE + SELL_VARIABLE_RATE) + 2 * FIXED_ORDER_COST
    required = max((notional * (1 + BUY_VARIABLE_RATE) + FIXED_ORDER_COST) / ALLOCATION_FRACTION,
                   planned_risk / PLANNED_RISK_FRACTION, planned_risk / daily_fraction)
    budgets = {
        "allocation": amount * ALLOCATION_FRACTION,
        "cash": cash,
        "planned_risk": amount * PLANNED_RISK_FRACTION,
        "remaining_daily_risk": max(D(0), amount * daily_fraction + realized),
    }
    fixed_by_code = {
        "allocation": FIXED_ORDER_COST,
        "cash": FIXED_ORDER_COST,
        "planned_risk": 2 * FIXED_ORDER_COST,
        "remaining_daily_risk": 2 * FIXED_ORDER_COST,
    }
    descriptions = {
        "allocation": "25% premium allocation",
        "cash": "available paper cash",
        "planned_risk": "1% of capital at the planned stop",
        "remaining_daily_risk": "remaining session loss budget",
        "ask_depth": "displayed ask size",
        "bid_depth": "displayed bid size",
        "freeze_limit": "exchange freeze quantity minus one",
    }
    capacities = [
        ("allocation", _budget_units(budgets["allocation"], entry_unit, fixed_by_code["allocation"])),
        ("cash", _budget_units(budgets["cash"], entry_unit, fixed_by_code["cash"])),
        ("planned_risk", _budget_units(budgets["planned_risk"], risk_unit, fixed_by_code["planned_risk"])),
        ("remaining_daily_risk", _budget_units(
            budgets["remaining_daily_risk"], risk_unit, fixed_by_code["remaining_daily_risk"])),
        ("ask_depth", int(quote["ask_size"])),
        ("bid_depth", int(quote["bid_size"])),
        ("freeze_limit", int(contract["freeze"]) - 1),
    ]
    raw_units = min(capacity for _, capacity in capacities)
    lot = int(contract["lot"])
    binding_codes = [code for code, capacity in capacities if capacity == raw_units]
    fee_limited = [
        code for code in binding_codes
        if code in fixed_by_code and budgets[code] <= fixed_by_code[code]
    ]
    binding = (["fee_reserve"] if fee_limited else []) + binding_codes
    observed = {
        **{code: str(budgets[code]) for code in budgets},
        "ask_depth": str(quote["ask_size"]),
        "bid_depth": str(quote["bid_size"]),
        "freeze_limit": str(int(contract["freeze"]) - 1),
        "fee_reserve": str(FIXED_ORDER_COST),
    }
    constraints = [
        {
            "code": code,
            "unit_capacity": capacity,
            "whole_lot_capacity": max(0, capacity // lot * lot),
            "binding": code in binding,
            "observed": observed[code],
            "limit": descriptions[code],
        }
        for code, capacity in capacities
    ]
    if fee_limited:
        constraints.insert(0, {
            "code": "fee_reserve",
            "unit_capacity": 0,
            "whole_lot_capacity": 0,
            "binding": True,
            "observed": str(min(budgets[code] for code in fee_limited)),
            "limit": "fixed order cost reserved before a lot can be bought",
        })
    return {
        "symbol": contract["symbol"],
        "whole_lot_quantity": quantity_for(state, contract, quote),
        "binding_constraint": binding[0],
        "binding_constraints": binding,
        "constraints": constraints,
        "lot_blocks_whole_lot": 0 < raw_units < lot,
        "minimum_initial_capital_estimate": str(required.quantize(D(".01"), rounding=ROUND_CEILING)),
        "minimum_cash": str(notional * (1 + BUY_VARIABLE_RATE) + FIXED_ORDER_COST),
        "lot": contract["lot"],
        "planned_lot_risk": str(planned_risk),
        "displayed_bid_units": quote["bid_size"],
        "displayed_ask_units": quote["ask_size"],
        "guaranteed_loss_cap": False,
        "note": "Estimate before later losses, gaps and liquidity changes; not a guaranteed loss cap",
    }
