"""Isolated, durable AI paper desk. Never calls broker order endpoints.

Network calls happen outside session locks. Exit monitoring commits before AI
inference; decisions are revalidated against current session state afterwards.
"""

from __future__ import annotations

import itertools
import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal as D
from uuid import uuid4

from .brokers.groww import BrokerApiError
from .chart_analysis import entry_evidence
from .intraday import IST, SignalMailer
from .options_ai import DAILY_BUDGET, MODEL, RESERVATION, TRIAL_BUDGET, OpenAIPaperAnalyst
from .options_market import BankNiftyMarket
from .options_risk import fees, fill_price
from .paper_session import validate_limits
from .playbooks import VERSION as SELECTOR_VERSION
from .playbooks import catalogue, select_plans, underlying_exit, validate_plan

DESK = "kiwit-banknifty-paper"


def fresh(quote, now):
    return quote is not None and 0 <= (now - datetime.fromisoformat(quote["stamp"])).total_seconds() <= 60


def entry_window(now):
    local = now.astimezone(IST)
    return local.weekday() < 5 and time(9, 30) <= local.time() < time(15, 0)


def market_window(now):
    local = now.astimezone(IST)
    return local.weekday() < 5 and time(9, 30) <= local.time() < time(15, 30)


class BankNiftyStore:
    def __init__(self, database):
        self.database = database

    @contextmanager
    def locked(self):
        with self.database.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", (DESK,))
            yield connection

    def latest(self, connection):
        row = connection.execute("SELECT state FROM banknifty_sessions ORDER BY trading_date DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def save(self, connection, state):
        connection.execute(
            "INSERT INTO banknifty_sessions(trading_date,state) VALUES(%s,%s::jsonb) "
            "ON CONFLICT(trading_date) DO UPDATE SET state=EXCLUDED.state,updated_at=now()",
            (state["day"], json.dumps(state)),
        )

    def event(self, connection, state, kind, detail):
        connection.execute(
            "INSERT INTO banknifty_events(trading_date,kind,detail) VALUES(%s,%s,%s::jsonb)",
            (state["day"], kind, json.dumps(detail, default=str)),
        )

    def halted(self, connection):
        connection.execute("LOCK TABLE system_halts IN SHARE MODE")
        return connection.execute(
            "SELECT EXISTS(SELECT 1 FROM system_halts WHERE active AND scope IN ('global',%s,'kiwit-paper-auto'))",
            (DESK,),
        ).fetchone()[0]

    def reserve(self, now, snapshot):
        with self.locked() as connection:
            state = self.latest(connection)
            if not state or state["state"] != "running" or state["day"] != str(now.astimezone(IST).date()):
                return None
            if self.halted(connection):
                return None
            day = state["day"]
            used, today = connection.execute(
                "SELECT COALESCE(sum(reserved_usd),0),COALESCE(sum(reserved_usd) "
                "FILTER(WHERE trading_date=%s),0) FROM banknifty_ai_calls",
                (day,),
            ).fetchone()
            if used + RESERVATION > TRIAL_BUDGET or today + RESERVATION > DAILY_BUDGET:
                state["detail"] = "AI spending limit reached; independent exits remain active"
                self.save(connection, state)
                return None
            call_id = uuid4()
            row = connection.execute(
                "INSERT INTO banknifty_ai_calls(call_id,trading_date,slot,state,reserved_usd,snapshot) "
                "VALUES(%s,%s,%s,'reserved',%s,%s::jsonb) ON CONFLICT(slot) DO NOTHING RETURNING call_id",
                (call_id, day, int(now.timestamp()) // 300, RESERVATION, json.dumps(snapshot)),
            ).fetchone()
            return call_id if row else None

    def settle(self, call_id, decision, usage):
        with self.locked() as connection:
            connection.execute(
                "UPDATE banknifty_ai_calls SET state=%s,reserved_usd=%s,result=%s::jsonb WHERE call_id=%s",
                (
                    "completed" if decision else "failed",
                    D(usage["budget_charge_usd"]) if usage else RESERVATION,
                    json.dumps({"decision": decision, "usage": usage}),
                    call_id,
                ),
            )

    def learning_context(self, connection, before_day):
        rows = connection.execute(
            "WITH trades AS (SELECT detail->>'position_id' position_id,detail->>'playbook_id' playbook_id,"
            "sum((detail->>'pnl')::numeric) pnl,max((detail->>'capital')::numeric) capital,"
            "bool_or((detail->>'closed')::boolean) closed FROM banknifty_events WHERE kind='paper_exit' "
            "AND trading_date<%s AND detail ? 'position_id' AND detail ? 'capital' "
            "GROUP BY detail->>'position_id',detail->>'playbook_id') "
            "SELECT playbook_id,count(*),count(*) FILTER(WHERE pnl>0),sum(pnl),"
            "avg(pnl/nullif(capital,0)*100) FROM trades WHERE closed GROUP BY playbook_id",
            (before_day,),
        ).fetchall()
        days = connection.execute(
            "SELECT trading_date,summary FROM banknifty_learning_days WHERE trading_date<%s "
            "ORDER BY trading_date DESC LIMIT 10",
            (before_day,),
        ).fetchall()
        return {
            "version": "banknifty-learning-v1",
            "mode": "bounded_in_context_evidence_not_model_training",
            "playbook_evidence": [
                {
                    "playbook_id": p,
                    "closed_trades": n,
                    "wins": w,
                    "net_pnl": str(pnl),
                    "mean_return_pct": str(mean),
                    "evidence_state": "exploratory" if n >= 20 else "collecting",
                    "promotion_eligible": False,
                }
                for p, n, w, pnl, mean in rows
            ],
            "recent_days": [{"day": str(day), "summary": summary} for day, summary in days],
            "limits": "Never edits playbooks, risk limits, code or live permissions; no automatic promotion",
        }

    def finalize_learning(self, connection, state):
        if state["state"] != "completed" or state["position"]:
            return
        counts = connection.execute(
            "SELECT kind,count(*) FROM banknifty_events WHERE trading_date=%s GROUP BY kind", (state["day"],)
        ).fetchall()
        summary = {
            "session_version": state["version"],
            "selector_version": SELECTOR_VERSION,
            "realized_pnl": state["realized_pnl"],
            "entries": state["entries"],
            "event_counts": {kind: count for kind, count in counts},
            "final_state": "reconciled_flat",
            "training": False,
        }
        connection.execute(
            "INSERT INTO banknifty_learning_days(trading_date,selector_version,summary) "
            "VALUES(%s,%s,%s::jsonb) ON CONFLICT(trading_date) DO NOTHING",
            (state["day"], SELECTOR_VERSION, json.dumps(summary)),
        )

    def daily_report(self, connection, state, now):
        row = connection.execute(
            "SELECT report,delivery_status,delivery_attempts,delivery_attempted_at,delivery_error "
            "FROM banknifty_daily_reports WHERE trading_date=%s",
            (state["day"],),
        ).fetchone()
        if row:
            report = row[0]
            report["delivery"] = {
                "status": row[1],
                "attempts": row[2],
                "attempted_at": str(row[3]) if row[3] else None,
                "error": row[4],
            }
            return report
        counts = connection.execute(
            "SELECT kind,count(*) FROM banknifty_events WHERE trading_date=%s GROUP BY kind", (state["day"],)
        ).fetchall()
        event_counts = {kind: count for kind, count in counts}
        realized = D(state["realized_pnl"])
        capital = D(state["amount"])
        report = {
            "version": "banknifty-daily-report-v1",
            "day": state["day"],
            "generated_at": now.isoformat(),
            "cutoff": "15:30 Asia/Kolkata",
            "execution": "paper-only",
            "session_state": state["state"],
            "reconciled_flat": state["position"] is None,
            "capital": state["amount"],
            "realized_pnl": state["realized_pnl"],
            "mark_pnl": state["pnl"],
            "return_pct": str(realized / capital * 100 if capital else D(0)),
            "entries": state["entries"],
            "loss_limit_pct": state["loss_pct"],
            "profit_target_pct": state["profit_pct"],
            "outcome": "profit" if realized > 0 else "loss" if realized < 0 else "flat",
            "open_position": (
                {
                    "symbol": state["position"]["contract"]["symbol"],
                    "quantity": state["position"]["quantity"],
                    "last_mark": state["position"].get("mark"),
                    "mark_at": state["position"].get("mark_at"),
                }
                if state["position"]
                else None
            ),
            "event_counts": event_counts,
        }
        connection.execute(
            "INSERT INTO banknifty_daily_reports(trading_date,generated_at,report) VALUES(%s,%s,%s::jsonb)",
            (state["day"], now, json.dumps(report)),
        )
        self.event(connection, state, "daily_report_generated", {"version": report["version"]})
        report["delivery"] = {"status": "pending", "attempts": 0, "attempted_at": None, "error": ""}
        return report


class BankNiftyService:
    def __init__(self, database, broker, *, market=None, analyst=None, clock=None, mailer=None):
        self.store = BankNiftyStore(database)
        self.market = market or (BankNiftyMarket(broker) if broker else None)
        self.analyst = analyst or OpenAIPaperAnalyst()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.mailer = mailer or SignalMailer()

    @property
    def enabled(self):
        return os.getenv("KIWIT_BANKNIFTY_AI_ENABLED", "false").lower() == "true"

    def start(self, amount, loss_pct, profit_pct, actor):
        now = self.clock()
        amount, loss, profit = validate_limits(amount, loss_pct, profit_pct)
        day = str(now.astimezone(IST).date())
        with self.store.locked() as connection:
            old = self.store.latest(connection)
            if old and old["state"] == "completed" and not old["position"]:
                self.store.finalize_learning(connection, old)
            if old and old["day"] == day:
                if tuple(map(D, (old["amount"], old["loss_pct"], old["profit_pct"]))) != (amount, loss, profit):
                    raise ValueError("Today’s paper limits are immutable")
                resumable = (
                    old["state"] == "completed"
                    and old["position"] is None
                    and old["entries"] == 0
                    and D(old["realized_pnl"]) == 0
                    and int(old.get("resumes", 0)) == 0
                    and now.astimezone(IST).time() < time(15)
                )
                if resumable:
                    old["state"] = "running"
                    old["resumes"] = 1
                    old["detail"] = "Audited same-day resume approved; original limits preserved"
                    old["last_tick"] = None
                    self.store.save(connection, old)
                    self.store.event(
                        connection,
                        old,
                        "run_resumed",
                        {"actor": actor, "reason": "completed flat with zero entries", "limits_preserved": True},
                    )
                return old
            if old and (old["position"] or old["state"] != "completed"):
                raise ValueError("Previous Bank Nifty session must be reconciled first")
            if now.astimezone(IST).weekday() >= 5 or now.astimezone(IST).time() >= time(15):
                raise ValueError("Start on a weekday before 15:00 IST")
            if not self.enabled or not os.getenv("OPENAI_API_KEY") or self.market is None:
                raise ValueError("Bank Nifty AI worker/key/read-only feed is not configured")
            if self.store.halted(connection):
                raise ValueError("Safety halt is active")
            state = {
                "day": day,
                "amount": str(amount),
                "loss_pct": str(loss),
                "profit_pct": str(profit),
                "cash": str(amount),
                "realized_pnl": "0",
                "pnl": "0",
                "entries": 0,
                "resumes": 0,
                "position": None,
                "state": "running",
                "actor": actor,
                "approved_at": now.isoformat(),
                "detail": "Run approved: warming up Bank Nifty data",
                "history": [],
                "last_exit": None,
                "execution": "paper-only",
                "model": MODEL,
                "version": "banknifty-ai-v4-playbooks",
                "last_tick": None,
            }
            self.store.save(connection, state)
            self.store.event(connection, state, "run_approved", {"actor": actor, "limits": state})
            return state

    def stop(self, actor):
        with self.store.locked() as connection:
            state = self.store.latest(connection)
            if state and state["state"] != "completed":
                state["state"] = "stopping" if state["position"] else "completed"
                state["detail"] = (
                    "Operator stop; waiting for executable exit quote" if state["position"] else "Stopped flat"
                )
                self.store.save(connection, state)
                self.store.event(connection, state, "stop_requested", {"actor": actor})
                self.store.finalize_learning(connection, state)
            return state or {"state": "idle"}

    def status(self):
        with self.store.locked() as connection:
            state = self.store.latest(connection)
            used = connection.execute("SELECT COALESCE(sum(reserved_usd),0) FROM banknifty_ai_calls").fetchone()[0]
            events = connection.execute(
                "SELECT event_at,kind,detail FROM banknifty_events ORDER BY event_id DESC LIMIT 40"
            ).fetchall()
            calls = connection.execute(
                "SELECT created_at,state,result FROM banknifty_ai_calls ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            review = connection.execute(
                "WITH trades AS (SELECT detail->>'position_id' AS position_id, "
                "detail->>'playbook_id' AS playbook_id, sum((detail->>'pnl')::numeric) AS pnl, "
                "bool_or((detail->>'closed')::boolean) AS closed FROM banknifty_events "
                "WHERE kind='paper_exit' AND detail ? 'position_id' "
                "GROUP BY detail->>'position_id',detail->>'playbook_id') "
                "SELECT playbook_id,count(*) FILTER(WHERE closed), "
                "count(*) FILTER(WHERE closed AND pnl>0), "
                "COALESCE(sum(pnl) FILTER(WHERE closed),0),sum(pnl),count(*) FILTER(WHERE NOT closed) "
                "FROM trades GROUP BY playbook_id"
            ).fetchall()
            learning = self.store.learning_context(connection, "9999-12-31")
            reports = connection.execute(
                "SELECT trading_date,generated_at,report,delivery_status,delivery_attempts,"
                "delivery_attempted_at,delivery_error FROM banknifty_daily_reports "
                "ORDER BY trading_date DESC LIMIT 10"
            ).fetchall()
        return {
            "available": self.enabled and self.market is not None and bool(os.getenv("OPENAI_API_KEY")),
            "execution": "paper-only",
            "model": MODEL,
            "selector_version": SELECTOR_VERSION,
            "playbooks": catalogue(),
            "paper_review": [
                {
                    "playbook_id": p,
                    "closed_trades": n,
                    "winning_trades": w,
                    "closed_net_pnl": str(net),
                    "realized_pnl_including_partial": str(total),
                    "partially_exited_trades": partial,
                }
                for p, n, w, net, total, partial in review
            ],
            "learning": learning,
            "daily_reports": [
                {
                    **report,
                    "day": str(day),
                    "generated_at": str(generated),
                    "delivery": {
                        "status": delivery,
                        "attempts": attempts,
                        "attempted_at": str(attempted) if attempted else None,
                        "error": error,
                    },
                }
                for day, generated, report, delivery, attempts, attempted, error in reports
            ],
            "session": {k: v for k, v in state.items() if k != "chart_cache"} if state else None,
            "budget": {
                "trial_limit_usd": str(TRIAL_BUDGET),
                "daily_limit_usd": str(DAILY_BUDGET),
                "used_or_reserved_usd": str(used),
                "accounting": "conservative estimate, not invoice",
            },
            "events": [{"at": str(at), "kind": kind, "detail": detail} for at, kind, detail in events],
            "decisions": [{"at": str(at), "state": status, "result": result} for at, status, result in calls],
        }

    def _process_daily_report(self, state, now):
        local = now.astimezone(IST)
        if not state or local.time() < time(15, 30) or state["day"] != str(local.date()):
            return None
        with self.store.locked() as connection:
            current = self.store.latest(connection)
            if not current or current["day"] != state["day"]:
                return None
            report = self.store.daily_report(connection, current, now)
            delivery = report["delivery"]
            should_send = delivery["status"] != "sent" and delivery["attempts"] < 3
        if not should_send:
            return report
        status, error = self.mailer.send_daily_report(
            report, os.getenv("KIWIT_DASHBOARD_URL", "https://kiwit.tathyaforge.in/dashboard")
        )
        with self.store.locked() as connection:
            connection.execute(
                "UPDATE banknifty_daily_reports SET delivery_status=%s,delivery_attempts=delivery_attempts+1,"
                "delivery_attempted_at=%s,delivery_error=%s WHERE trading_date=%s AND delivery_status<>'sent'",
                (status, now, error[:500], state["day"]),
            )
            current = self.store.latest(connection)
            if current:
                self.store.event(connection, current, "daily_report_delivery", {"status": status, "error": error[:200]})
        return report

    def _close(self, connection, state, quote, reason, now):
        position = state["position"]
        if not position or not market_window(now) or not fresh(quote, now):
            return False
        if position.get("last_exit_quote") == quote["stamp"]:
            return False
        # Partial fills limited to displayed bid depth, always whole lots.
        qty = min(position["quantity"], quote["bid_size"]) // position["contract"]["lot"] * position["contract"]["lot"]
        if qty <= 0:
            return False
        price = fill_price(quote, position["contract"], False)
        if price <= 0:
            return False
        proceeds = price * qty - fees(price * qty)
        remaining_cost = D(
            position.get("entry_cost_remaining", D(position["entry_cost_per_unit"]) * position["quantity"])
        )
        entry_cost = (
            remaining_cost if qty == position["quantity"] else remaining_cost * D(qty) / D(position["quantity"])
        )
        state["cash"] = str(D(state["cash"]) + proceeds)
        state["realized_pnl"] = str(D(state["realized_pnl"]) + proceeds - entry_cost)
        position["entry_cost_remaining"] = str(remaining_cost - entry_cost)
        position["quantity"] -= qty
        position["last_exit_quote"] = quote["stamp"]
        self.store.event(
            connection,
            state,
            "paper_exit",
            {
                "symbol": position["contract"]["symbol"],
                "quantity": qty,
                "price": str(price),
                "reason": reason,
                "pnl": str(proceeds - entry_cost),
                "capital": state["amount"],
                "quote": quote,
                "position_id": position["id"],
                "playbook_id": position.get("entry_plan", {}).get("playbook_id", "legacy_unattributed"),
                "closed": position["quantity"] == 0,
            },
        )
        if not position["quantity"]:
            state["position"] = None
            state["last_exit"] = now.isoformat()
            state["pnl"] = state["realized_pnl"]
            if state["state"] == "stopping":
                state["state"] = "completed"
        else:
            position["exit_pending"] = reason
        return True

    def _monitor(self):
        now = self.clock()
        with self.store.locked() as connection:
            initial = self.store.latest(connection)
        if not initial or initial["state"] == "completed":
            return initial
        position, quote, underlying = initial["position"], None, None
        if position and self.market:
            try:
                quote = self.market.quote(position["contract"]["symbol"], self.clock(), entry=False)
            except (BrokerApiError, OSError, ValueError, ArithmeticError):
                quote = None  # no invented price; persist stale valuation below
            # Price-based exits remain available even if underlying data is unavailable.
            if position.get("entry_plan") and fresh(quote, self.clock()):
                try:
                    underlying = self.market.latest_underlying(self.clock())
                except (BrokerApiError, OSError, ValueError, ArithmeticError):
                    underlying = None
        now = self.clock()
        with self.store.locked() as connection:
            state = self.store.latest(connection)
            if not state or state["state"] == "completed":
                return state
            state["last_tick"] = now.isoformat()
            local = now.astimezone(IST)
            if state["day"] != str(local.date()) or local.time() >= time(15, 15):
                state["state"] = "stopping"
                state["detail"] = "End of day: flattening/reconciliation"
            if not self.enabled or self.store.halted(connection):
                state["state"] = "stopping"
                state["detail"] = "Safety halt or worker disabled; closing paper positions"
            current = state["position"]
            if current and position and current["id"] == position["id"]:
                state["valuation_fresh"] = fresh(quote, now)
                if fresh(quote, now):
                    price = fill_price(quote, current["contract"], False)
                    value = price * current["quantity"]
                    state["pnl"] = str(D(state["cash"]) + value - fees(value) - D(state["amount"]))
                    current["mark"] = str(price)
                    current["mark_at"] = quote["stamp"]
                    current["underlying_check"] = underlying
                    pnl, amount = D(state["pnl"]), D(state["amount"])
                    if pnl <= -amount * D(state["loss_pct"]) / 100 or pnl >= amount * D(state["profit_pct"]) / 100:
                        state["state"] = "stopping"
                        state["detail"] = "Session P&L limit triggered"
                    reason = (
                        "session_stop"
                        if state["state"] == "stopping"
                        else current.get("exit_pending")
                        or (
                            "stop_loss"
                            if price <= D(current["stop"])
                            else "take_profit"
                            if price >= D(current["target"])
                            else "underlying_invalidation"
                            if underlying_exit(current, underlying, now)
                            else "time_exit"
                            if current.get("exit_deadline") and now >= datetime.fromisoformat(current["exit_deadline"])
                            else None
                        )
                    )
                    if reason:
                        self._close(connection, state, quote, reason, now)
                else:
                    state["detail"] = "Position quote unavailable/stale; no new entry or fabricated exit"
            if state["position"] is None:
                state["valuation_fresh"] = True
                state["pnl"] = state["realized_pnl"]
                pnl, amount = D(state["pnl"]), D(state["amount"])
                if (
                    state["state"] == "stopping"
                    or state["entries"] >= 10
                    or pnl <= -amount * D(state["loss_pct"]) / 100
                    or pnl >= amount * D(state["profit_pct"]) / 100
                ):
                    state["state"] = "completed"
                    state["detail"] = "Session complete; reconciled flat"
            self.store.save(connection, state)
            self.store.finalize_learning(connection, state)
            return state

    def _apply(self, decision, snapshot, call_id):
        now = self.clock()
        selected = next((c for c in snapshot["candidates"] if c["symbol"] == decision["symbol"]), None)
        quote, underlying = None, None
        if decision["action"] in ("BUY", "EXIT"):
            if decision["action"] == "BUY" and selected is None:
                raise ValueError("Model selected a contract outside its supplied universe")
            if decision["action"] == "BUY":
                underlying = self.market.latest_underlying(now)
            if decision["action"] == "EXIT" and (
                not snapshot["position"] or snapshot["position"]["contract"]["symbol"] != decision["symbol"]
            ):
                raise ValueError("EXIT must reference the existing long position")
            quote = self.market.quote(decision["symbol"], now, entry=decision["action"] == "BUY")
        now = self.clock()
        with self.store.locked() as connection:
            state = self.store.latest(connection)
            if not state or state["day"] != snapshot["day"] or state["state"] != "running":
                return
            applied = connection.execute(
                "UPDATE banknifty_ai_calls SET state='applied' "
                "WHERE call_id=%s AND state='completed' RETURNING call_id",
                (call_id,),
            ).fetchone()
            if not applied:
                return
            self.store.event(connection, state, "ai_decision", {"call_id": str(call_id), **decision})
            state["detail"] = decision["summary"]
            state["last_decision"] = {"at": now.isoformat(), **decision}
            if decision["action"] == "EXIT":
                if (
                    state["position"]
                    and snapshot["position"]
                    and state["position"]["id"] == snapshot["position"]["id"]
                    and state["position"]["contract"]["symbol"] == decision["symbol"]
                ):
                    self._close(connection, state, quote, "ai_exit", now)
            elif decision["action"] == "BUY":
                if (
                    state["position"]
                    or not entry_window(now)
                    or self.store.halted(connection)
                    or not self.enabled
                    or state["entries"] >= 10
                    or decision["strategy"] == "no_trade"
                ):
                    raise ValueError("Current session state blocks entry")
                if state["last_exit"] and (now - datetime.fromisoformat(state["last_exit"])).total_seconds() < 300:
                    raise ValueError("Five-minute cooldown after exit")
                if (
                    not fresh(quote, now)
                    or not 0 <= (now - datetime.fromisoformat(snapshot["spot_at"])).total_seconds() <= 120
                ):
                    raise ValueError("Decision or execution quote is stale")
                pnl, amount = D(state["realized_pnl"]), D(state["amount"])
                if pnl <= -amount * D(state["loss_pct"]) / 100 or pnl >= amount * D(state["profit_pct"]) / 100:
                    raise ValueError("Session P&L limit blocks entry")
                if selected["expiry"] <= state["day"]:
                    raise ValueError("Expiry-day contracts not permitted")
                evidence = entry_evidence(snapshot.get("chart_analysis"), selected["kind"], decision["strategy"], now)
                if not evidence:
                    raise ValueError("No fresh matching chart setup; paper entry blocked")
                plan = validate_plan(decision, snapshot, state, quote, underlying, now)
                qty = plan["quantity"]
                fill = fill_price(quote, selected, True)
                cost = fill * qty + fees(fill * qty)
                state["cash"] = str(D(state["cash"]) - cost)
                state["position"] = {
                    "id": str(call_id),
                    "contract": {k: v for k, v in selected.items() if k != "quote"},
                    "quantity": qty,
                    "entry": str(fill),
                    "entry_cost_per_unit": str(cost / qty),
                    "entry_cost_remaining": str(cost),
                    "stop": str(fill * (1 - D(state["loss_pct"]) / 100)),
                    "target": str(fill * (1 + D(state["profit_pct"]) / 100)),
                    "entered_at": now.isoformat(),
                    "entry_plan": plan,
                    "entry_underlying": underlying,
                    "exit_deadline": (now + timedelta(minutes=plan["max_hold_minutes"])).isoformat(),
                }
                state["entries"] += 1
                self.store.event(
                    connection,
                    state,
                    "paper_entry",
                    {
                        "call_id": str(call_id),
                        "position": state["position"],
                        "quote": quote,
                        "strategy": decision["strategy"],
                        "chart_evidence": evidence,
                    },
                )
            self.store.save(connection, state)

    def run_once(self):
        state = self._monitor()  # exits do not depend on model availability or remaining API credit
        now = self.clock()
        self._process_daily_report(state, now)
        if not state or state["state"] != "running" or not self.enabled or not entry_window(now):
            return {"state": state["state"] if state else "idle", "execution": "paper-only"}
        call_id = None
        try:
            snapshot = (
                self.market.snapshot(now, cached_context=state.get("chart_cache"))
                if isinstance(self.market, BankNiftyMarket)
                else self.market.snapshot(now)
            )
            # Quotes may arrive after scan start; evaluate them against receipt time.
            now = self.clock()
            with self.store.locked() as connection:
                current = self.store.latest(connection)
                if current["day"] != state["day"] or current["state"] != "running":
                    return {"state": "stopped"}
                history = current["history"]
                sample = {"at": snapshot["spot_at"], "spot": snapshot["spot"]}
                if snapshot.get("underlying_history"):
                    history = snapshot.pop("underlying_history")
                elif not history or history[-1]["at"] != sample["at"]:
                    history.append(sample)
                current["history"] = history[-20:]
                cache = snapshot.pop("chart_cache", None)
                analysis = snapshot.get("chart_analysis")
                if (
                    cache
                    and len(cache.get("daily", [])) >= 5
                    and not cache.get("partial_sessions")
                    and cache.get("previous_calendar_week", {}).get("coverage", {}).get("status") == "complete"
                ):
                    current["chart_cache"] = cache
                if analysis:
                    current["chart_analysis"] = analysis
                    # Chart bars stay in the session for rendering, not in the paid AI prompt.
                    snapshot["chart_analysis"] = {k: v for k, v in analysis.items() if k != "chart_bars"}
                current["detail"] = (
                    "Warming up: five fresh underlying observations required"
                    if len(history) < 5
                    else "Scanning Bank Nifty"
                )
                self.store.save(connection, current)
                snapshot.update(
                    day=current["day"],
                    history=current["history"],
                    position=current["position"],
                    capital=current["amount"],
                    loss_pct=current["loss_pct"],
                    profit_pct=current["profit_pct"],
                    cash=current["cash"],
                    realized_pnl=current["realized_pnl"],
                    entries=current["entries"],
                )
                snapshot["learning_context"] = self.store.learning_context(connection, current["day"])
                selection = select_plans(snapshot, current, now)
                current["strategy_selection"] = selection
                snapshot["strategy_selection"] = selection
                if not selection["plans"] and not current["position"]:
                    current["detail"] = "No eligible entry plan; waiting for a supported setup"
                self.store.save(connection, current)
                self.store.event(connection, current, "strategy_scan", selection)
                recent = connection.execute(
                    "SELECT state,result->'decision' FROM banknifty_ai_calls WHERE trading_date=%s "
                    "AND result->'decision' IS NOT NULL ORDER BY created_at DESC LIMIT 3",
                    (current["day"],),
                ).fetchall()
                snapshot["previous_decisions"] = [
                    {
                        "status": status,
                        "action": decision["action"],
                        "symbol": decision["symbol"],
                        "summary": decision["summary"][:600],
                    }
                    for status, decision in recent
                    if decision
                ]
            if len(snapshot["history"]) < 5:
                return {"state": "warming_up"}
            if analysis and not analysis["ready"]:
                raise ValueError("Chart context incomplete: " + "; ".join(analysis["issues"]))
            if not snapshot["candidates"]:
                raise ValueError("No fresh liquid Bank Nifty option candidates")
            if not selection["plans"] and not snapshot["position"]:
                return {"state": "waiting_for_setup", "ai_called": False}
            times = [datetime.fromisoformat(item["at"]) for item in snapshot["history"][-5:]]
            if any(not 0 < (b - a).total_seconds() <= 120 for a, b in itertools.pairwise(times)):
                raise ValueError("Underlying history has gaps")
            call_id = self.store.reserve(self.clock(), snapshot)
            if call_id:
                try:
                    decision, usage = self.analyst.decide(snapshot)
                except (OSError, TimeoutError, ValueError, ArithmeticError):
                    self.store.settle(call_id, None, None)
                    raise ValueError("AI unavailable/incomplete; no order, reservation retained") from None
                self.store.settle(call_id, decision, usage)
                self._apply(decision, snapshot, call_id)
            return {"state": "running", "ai_called": bool(call_id)}
        except (BrokerApiError, OSError, TimeoutError, ValueError, ArithmeticError) as error:
            with self.store.locked() as connection:
                current = self.store.latest(connection)
                detail = str(error) if type(error) is ValueError else "Market data or AI unavailable"
                if call_id:
                    connection.execute(
                        "UPDATE banknifty_ai_calls SET state='rejected', "
                        "result=jsonb_set(result,'{validation_error}',%s::jsonb) "
                        "WHERE call_id=%s AND state='completed'",
                        (json.dumps(detail), call_id),
                    )
                # Never log provider errors/bodies containing authorization material.
                if current and current["state"] == "running":
                    current["detail"] = detail
                    self.store.save(connection, current)
                    self.store.event(
                        connection, current, "blocked", {"reason": detail, "call_id": str(call_id) if call_id else None}
                    )
            return {"state": "blocked", "detail": detail}
