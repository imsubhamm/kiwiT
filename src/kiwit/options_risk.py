"""Deterministic paper fill and sizing rules, shared by plans and execution."""

from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D

FEE_RATE = D(".002")  # illustrative, not exact statutory fees


def trade_limits(state):
    return (D(state.get("trade_stop_pct", state["loss_pct"])),
            D(state.get("trade_target_pct", state["profit_pct"])))


def session_limit_reached(state, pnl):
    amount = D(state["amount"])
    return (pnl <= -amount * D(state["loss_pct"]) / 100 or
            (state.get("session_profit_cap_enabled", True) and
             pnl >= amount * D(state["profit_pct"]) / 100))


def fees(notional):
    return notional * FEE_RATE + 20


def fill_price(quote, contract, buy):
    tick = D(contract["tick"])
    value = D(quote["ask" if buy else "bid"]) * (D("1.001") if buy else D(".999"))
    return (value / tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR) * tick


def quantity_for(state, contract, quote):
    fill = fill_price(quote, contract, True)
    amount, cash, loss = D(state["amount"]), D(state["cash"]), D(state["loss_pct"]) / 100
    allocation = min(amount / 2, cash)
    risk_budget = min(amount * D(".02"), max(D(0), amount * loss + D(state.get("realized_pnl", "0"))))
    units = min(
        int(max(D(0), allocation - 20) / (fill * (1 + FEE_RATE))),
        int(max(D(0), risk_budget - 40) / (fill * (D(state.get("trade_stop_pct", state["loss_pct"])) / 100 + 2 * FEE_RATE))),
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
    planned_risk = notional * (stop_pct + 2 * FEE_RATE) + 40
    daily_fraction = D(state['loss_pct']) / 100
    required = max((notional * (1 + FEE_RATE) + 20) * 2,
                   planned_risk / D('.02'), planned_risk / daily_fraction)
    return {'whole_lot_quantity': quantity_for(state, contract, quote),
            'minimum_initial_capital_estimate': str(required.quantize(D('.01'), rounding=ROUND_CEILING)),
            'minimum_cash': str(notional * (1 + FEE_RATE) + 20),
            'lot': contract['lot'], 'planned_lot_risk': str(planned_risk),
            'displayed_bid_units': quote['bid_size'], 'displayed_ask_units': quote['ask_size'],
            'note': 'Estimate before realized losses, gaps and liquidity changes; not a guaranteed loss cap'}
