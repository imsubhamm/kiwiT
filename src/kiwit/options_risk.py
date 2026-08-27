"""Deterministic paper fill and sizing rules, shared by plans and execution."""

from decimal import ROUND_CEILING, ROUND_FLOOR
from decimal import Decimal as D

FEE_RATE = D(".002")  # illustrative, not exact statutory fees


def fees(notional):
    return notional * FEE_RATE + 20


def fill_price(quote, contract, buy):
    tick = D(contract["tick"])
    value = D(quote["ask" if buy else "bid"]) * (D("1.001") if buy else D(".999"))
    return (value / tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR) * tick


def quantity_for(state, contract, quote):
    fill = fill_price(quote, contract, True)
    amount, cash, loss = D(state["amount"]), D(state["cash"]), D(state["loss_pct"]) / 100
    allocation = min(amount / 4, cash)
    risk_budget = min(amount * D(".01"), max(D(0), amount * loss + D(state.get("realized_pnl", "0"))))
    units = min(
        int(max(D(0), allocation - 20) / (fill * (1 + FEE_RATE))),
        int(max(D(0), risk_budget - 40) / (fill * (loss + 2 * FEE_RATE))),
        quote["ask_size"],
        quote["bid_size"],
        contract["freeze"] - 1,
    )
    return max(0, units // contract["lot"] * contract["lot"])
