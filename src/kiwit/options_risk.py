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


def sizing_diagnostics(state, contract, quote):
    """Explain infeasible whole-lot sizing without changing risk authority."""
    fill = fill_price(quote, contract, True)
    notional = fill * contract['lot']
    stop_pct = trade_limits(state)[0] / 100
    planned_risk = notional * (stop_pct + BUY_VARIABLE_RATE + SELL_VARIABLE_RATE) + 2 * FIXED_ORDER_COST
    daily_fraction = D(state['loss_pct']) / 100
    required = max((notional * (1 + BUY_VARIABLE_RATE) + FIXED_ORDER_COST) / ALLOCATION_FRACTION,
                   planned_risk / PLANNED_RISK_FRACTION, planned_risk / daily_fraction)
    return {'whole_lot_quantity': quantity_for(state, contract, quote),
            'minimum_initial_capital_estimate': str(required.quantize(D('.01'), rounding=ROUND_CEILING)),
            'minimum_cash': str(notional * (1 + BUY_VARIABLE_RATE) + FIXED_ORDER_COST),
            'lot': contract['lot'], 'planned_lot_risk': str(planned_risk),
            'displayed_bid_units': quote['bid_size'], 'displayed_ask_units': quote['ask_size'],
            'note': 'Estimate before realized losses, gaps and liquidity changes; not a guaranteed loss cap'}
