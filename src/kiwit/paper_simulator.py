"""Transactional, quote-driven paper lifecycle. No live broker capability."""

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Context, Decimal, localcontext
from pathlib import Path

from .domain import Decision, Side
from .marketdata.canonical import IST, utc
from .ml.datasets import _hash, _json

VERSION = "quote-paper-v1"


@dataclass(frozen=True)
class FillRules:
    slippage_fraction: Decimal = Decimal("0.0005")
    fee_fraction: Decimal = Decimal("0.0003")
    maximum_quote_age_seconds: int = 5

    def __post_init__(self):
        if any(not v.is_finite() or not 0 <= v < 1 for v in (self.slippage_fraction, self.fee_fraction)):
            raise ValueError("Invalid fill costs")
        if type(self.maximum_quote_age_seconds) is not int or self.maximum_quote_age_seconds < 0:
            raise ValueError("Invalid quote age")


class PaperSimulator:
    def __init__(self, path, *, initial_capital, calendar, rules=None):
        self.path, self.calendar, self.rules = Path(path), calendar, rules or FillRules()
        if not isinstance(initial_capital, Decimal) or not initial_capital.is_finite() or initial_capital <= 0:
            raise ValueError("Positive Decimal capital required")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        metadata = {
            "version": VERSION,
            "calendar": calendar.version,
            "initial_capital": str(initial_capital),
            "rules": {k: str(v) if isinstance(v, Decimal) else v for k, v in asdict(self.rules).items()},
        }
        with self._db() as db:
            db.executescript("""CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY, payload TEXT, digest TEXT);
                CREATE TABLE IF NOT EXISTS events (event_id TEXT PRIMARY KEY, request TEXT, result TEXT, digest TEXT);""")
            row = db.execute("SELECT payload,digest FROM state WHERE id=1").fetchone()
            if row:
                if self._verified(row)["metadata"] != metadata:
                    raise ValueError("Persistent simulator configuration mismatch")
            else:
                state = {
                    "metadata": metadata,
                    "cash": str(initial_capital),
                    "realized_pnl": "0",
                    "orders": {},
                    "clock": None,
                }
                db.execute("INSERT INTO state VALUES (1,?,?)", (_json(state), _hash(state)))

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _verified(row):
        value = json.loads(row[0])
        if _hash(value) != row[1]:
            raise ValueError("Simulator checksum mismatch")
        return value

    def _event(self, event_id, request, apply):
        if not event_id:
            raise ValueError("Event identity required")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT request,result,digest FROM events WHERE event_id=?", (event_id,)).fetchone()
            encoded = _json(request)
            if old:
                if old[0] != encoded:
                    raise ValueError("Conflicting event retry")
                return self._verified(old[1:])
            state = self._verified(db.execute("SELECT payload,digest FROM state WHERE id=1").fetchone())
            now = datetime.fromisoformat(request["at"])
            if state["clock"] and now < datetime.fromisoformat(state["clock"]):
                raise ValueError("Events must be chronological")
            with localcontext(Context(prec=50)):
                result = apply(state)
            state["clock"] = request["at"]
            db.execute("UPDATE state SET payload=?,digest=? WHERE id=1", (_json(state), _hash(state)))
            db.execute("INSERT INTO events VALUES (?,?,?,?)", (event_id, encoded, _json(result), _hash(result)))
            return result

    def submit(self, proposal, risk, *, at):
        at = utc(at)
        session = self.calendar.session(at.astimezone(IST).date())
        if session is None or not session.bounds()[0] <= at < session.bounds()[1]:
            raise ValueError("Cannot submit outside known session")
        if (
            risk.decision != Decision.APPROVE
            or risk.proposal_id != proposal.proposal_id
            or type(risk.quantity) is not int
            or risk.quantity <= 0
            or risk.quantity % proposal.instrument.lot_size
            or utc(proposal.signal_timestamp) > at
        ):
            raise ValueError("Matching approved lot quantity required")
        values = (proposal.entry_price, proposal.stop_price, proposal.target_price, risk.risk_budget)
        if any(not isinstance(v, Decimal) or not v.is_finite() or v <= 0 for v in values):
            raise ValueError("Finite prices and budget required")
        long = proposal.side == Side.BUY
        if proposal.side not in {Side.BUY, Side.SELL} or not (
            proposal.stop_price < proposal.entry_price < proposal.target_price
            if long
            else proposal.target_price < proposal.entry_price < proposal.stop_price
        ):
            raise ValueError("Invalid proposal geometry")
        order = {
            "id": str(proposal.proposal_id),
            "instrument": asdict(proposal.instrument),
            "long": long,
            "quantity": risk.quantity,
            "stop": str(proposal.stop_price),
            "target": str(proposal.target_price),
            "risk_budget": str(risk.risk_budget),
            "submitted_at": at.isoformat(),
            "session_end": session.bounds()[1].isoformat(),
            "status": "PENDING",
            "fills": [],
            "reason": "AWAITING_QUOTE",
        }
        order["instrument"]["tick_size"] = str(proposal.instrument.tick_size)

        def apply(state):
            if order["id"] in state["orders"]:
                raise ValueError("Order already exists")
            state["orders"][order["id"]] = order
            return order

        return self._event("submit:" + order["id"], {"at": at.isoformat(), "order": order}, apply)

    def cancel(self, order_id, *, at):
        def apply(state):
            order = state["orders"][order_id]
            if order["status"] != "PENDING":
                raise ValueError("Only pending orders can be cancelled")
            order.update(status="CANCELLED", reason="CANCEL_REQUEST")
            return order

        return self._event("cancel:" + order_id, {"at": utc(at).isoformat(), "order_id": order_id}, apply)

    def end_session(self, *, at):
        at = utc(at)

        def apply(state):
            for order in state["orders"].values():
                if at >= datetime.fromisoformat(order["session_end"]):
                    if order["status"] == "PENDING":
                        order.update(status="CANCELLED", reason="SESSION_ENDED")
                    elif order["status"] == "OPEN":
                        order.update(status="EXIT_PENDING", reason="SESSION_EXIT_AWAITING_QUOTE")
            return state["orders"]

        return self._event("session:" + at.isoformat(), {"at": at.isoformat()}, apply)

    def on_quote(self, event_id, quote, *, received_at):
        now, quoted = utc(received_at), utc(quote.timestamp)
        if not 0 <= (now - quoted).total_seconds() <= self.rules.maximum_quote_age_seconds:
            raise ValueError("Quote unavailable, stale or future")
        if any(not p.is_finite() or p <= 0 for p in (quote.bid, quote.ask)) or quote.ask < quote.bid:
            raise ValueError("Invalid executable quote")
        request = {
            "at": now.isoformat(),
            "quote_at": quoted.isoformat(),
            "instrument": {**asdict(quote.instrument), "tick_size": str(quote.instrument.tick_size)},
            "bid": str(quote.bid),
            "ask": str(quote.ask),
        }

        def price(buy):
            raw = (quote.ask if buy else quote.bid) * (
                1 + self.rules.slippage_fraction if buy else 1 - self.rules.slippage_fraction
            )
            tick = quote.instrument.tick_size
            value = (raw / tick).to_integral_value(rounding=ROUND_CEILING if buy else ROUND_FLOOR) * tick
            if value <= 0:
                raise ValueError("Unrepresentable executable price")
            return value

        def apply(state):
            changed = []
            instrument = asdict(quote.instrument)
            instrument["tick_size"] = str(quote.instrument.tick_size)
            for order in state["orders"].values():
                if order["instrument"] != instrument or order["status"] not in {"PENDING", "OPEN", "EXIT_PENDING"}:
                    continue
                q, long = order["quantity"], order["long"]
                ending = now >= datetime.fromisoformat(order["session_end"])
                if order["status"] == "PENDING":
                    if ending:
                        order.update(status="CANCELLED", reason="SESSION_ENDED")
                    elif quoted < datetime.fromisoformat(order["submitted_at"]):
                        continue
                    else:
                        fill = price(long)
                        fee = fill * q * self.rules.fee_fraction
                        tick = quote.instrument.tick_size
                        projected_stop = Decimal(order["stop"]) * (
                            1 - self.rules.slippage_fraction if long else 1 + self.rules.slippage_fraction
                        )
                        projected_stop = (projected_stop / tick).to_integral_value(
                            rounding=ROUND_FLOOR if long else ROUND_CEILING
                        ) * tick
                        risk = abs(fill - projected_stop) * q + fee + projected_stop * q * self.rules.fee_fraction
                        geometry = (
                            Decimal(order["stop"]) < fill < Decimal(order["target"])
                            if long
                            else Decimal(order["target"]) < fill < Decimal(order["stop"])
                        )
                        if (
                            not geometry
                            or risk > Decimal(order["risk_budget"])
                            or fill * q + fee > Decimal(state["cash"])
                        ):
                            order.update(status="REJECTED", reason="FILL_RISK_OR_CAPITAL_GATE")
                        else:
                            state["cash"] = str(Decimal(state["cash"]) - fill * q - fee)
                            order.update(
                                status="OPEN",
                                entry=str(fill),
                                entry_fee=str(fee),
                                collateral=str(fill * q),
                                mark=str(quote.bid if long else quote.ask),
                                reason="FILLED",
                            )
                            order["fills"].append(
                                {"at": now.isoformat(), "price": str(fill), "fee": str(fee), "kind": "ENTRY"}
                            )
                else:
                    mark = quote.bid if long else quote.ask
                    order["mark"] = str(mark)
                    stop = mark <= Decimal(order["stop"]) if long else mark >= Decimal(order["stop"])
                    target = mark >= Decimal(order["target"]) if long else mark <= Decimal(order["target"])
                    if stop or target or ending or order["status"] == "EXIT_PENDING":
                        fill = price(not long)
                        fee = fill * q * self.rules.fee_fraction
                        gross = (fill - Decimal(order["entry"])) * q * (1 if long else -1)
                        net = gross - Decimal(order["entry_fee"]) - fee
                        state["cash"] = str(Decimal(state["cash"]) + Decimal(order["collateral"]) + gross - fee)
                        state["realized_pnl"] = str(Decimal(state["realized_pnl"]) + net)
                        order.update(
                            status="CLOSED",
                            realized_pnl=str(net),
                            reason="STOP" if stop else "TARGET" if target else "SESSION_EXIT",
                        )
                        order["fills"].append(
                            {"at": now.isoformat(), "price": str(fill), "fee": str(fee), "kind": "EXIT"}
                        )
                changed.append(order)
            return changed

        return self._event(event_id, request, apply)

    def portfolio(self):
        with self._db() as db:
            state = self._verified(db.execute("SELECT payload,digest FROM state WHERE id=1").fetchone())
        equity, unrealized = Decimal(state["cash"]), Decimal(0)
        for order in state["orders"].values():
            if order["status"] in {"OPEN", "EXIT_PENDING"}:
                gross = (
                    (Decimal(order["mark"]) - Decimal(order["entry"]))
                    * order["quantity"]
                    * (1 if order["long"] else -1)
                )
                equity += Decimal(order["collateral"]) + gross
                unrealized += gross - Decimal(order["entry_fee"])
        return {**state, "equity": str(equity), "unrealized_pnl": str(unrealized)}
