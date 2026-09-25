"""Isolated, durable AI paper desk. Never calls broker order endpoints.

Network calls happen outside session locks. Exit monitoring commits before AI
inference; decisions are revalidated against current session state afterwards.
"""

from __future__ import annotations

import hashlib
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
from .options_ai import (
    BUDGET_DAYS,
    DAILY_BUDGET,
    MODEL,
    RESERVATION,
    ROLLING_BUDGET,
    AIFailure,
    OpenAIPaperAnalyst,
    failure_evidence,
    provenance,
    provider_ready,
)
from .options_calendar import regular_session
from .options_events import event_context
from .options_market import BankNiftyMarket
from .options_operations import diagnostics, heartbeat, report_backlog
from .options_policy import ENTRY_CAP, decision_event, entry_gate
from .options_risk import (
    BROKER_COST_VERSION,
    cost_breakdown,
    exit_levels,
    fees,
    fill_price,
    session_limit_reached,
    trade_limits,
)
from .paper_session import validate_limits
from .playbooks import VERSION as SELECTOR_VERSION
from .playbooks import catalogue, select_plans, underlying_exit, validate_plan

DESK = "kiwit-banknifty-paper"


def market_failure_evidence(error, operation="snapshot"):
    if isinstance(error, BrokerApiError):
        code = "BROKER_PROVIDER_ERROR"
    elif isinstance(error, (OSError, TimeoutError)):
        code = "MARKET_TRANSPORT_ERROR"
    elif isinstance(error, ArithmeticError):
        code = "MARKET_NUMERIC_VALIDATION_ERROR"
    else:
        message = str(error).lower()
        code = (
            "UNDERLYING_CANDLES_STALE" if "candle" in message or "stale" in message
            else "INSTRUMENT_MASTER_ERROR" if "instrument" in message or "contract" in message
            else "MARKET_SNAPSHOT_VALIDATION_ERROR"
        )
    message = str(error).replace("\n", " ")[:300]
    return {"reason_code": code, "operation": operation, "error_type": type(error).__name__,
            "safe_message": message or code}


def experiment_id(state=None):
    state = state or {}
    risk = {key: str(state.get(key)) for key in (
        "amount", "loss_pct", "profit_pct", "trade_stop_pct", "trade_target_pct", "session_profit_cap_enabled")}
    compatible_provenance = {key: value for key, value in provenance().items() if key != "release"}
    return hashlib.sha256(json.dumps({**compatible_provenance, "selector": SELECTOR_VERSION,
                                     "cost_model": BROKER_COST_VERSION, "risk": risk},
                                     sort_keys=True).encode()).hexdigest()[:20]


def fresh(quote, now):
    return quote is not None and 0 <= (now - datetime.fromisoformat(quote["stamp"])).total_seconds() <= 90


def entry_window(now):
    local = now.astimezone(IST)
    return regular_session(local.date()) is True and time(9, 30) <= local.time() < time(15, 0)


def market_window(now):
    local = now.astimezone(IST)
    return regular_session(local.date()) is True and time(9, 30) <= local.time() < time(15, 30)


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

    def record_market_snapshot(self, connection, state, snapshot, selection):
        """Keep an auditable time series separate from decision and trade records."""
        connection.execute(
            "INSERT INTO banknifty_market_history("
            "trading_date,observed_at,spot,market_snapshot,strategy_selection,scan_state) "
            "VALUES(%s,%s,%s,%s::jsonb,%s::jsonb,%s) "
            "ON CONFLICT(trading_date,observed_at) DO UPDATE SET "
            "spot=CASE WHEN EXCLUDED.scan_state='live_observation' THEN EXCLUDED.spot "
            "ELSE banknifty_market_history.spot END,"
            "market_snapshot=CASE "
            "WHEN EXCLUDED.market_snapshot ? 'decision_event_key' "
            "THEN banknifty_market_history.market_snapshot || EXCLUDED.market_snapshot "
            "WHEN banknifty_market_history.market_snapshot ? 'decision_event_key' "
            "THEN EXCLUDED.market_snapshot || banknifty_market_history.market_snapshot "
            "ELSE EXCLUDED.market_snapshot END,"
            "strategy_selection=CASE "
            "WHEN EXCLUDED.market_snapshot ? 'decision_event_key' THEN EXCLUDED.strategy_selection "
            "WHEN banknifty_market_history.market_snapshot ? 'decision_event_key' "
            "THEN banknifty_market_history.strategy_selection "
            "ELSE EXCLUDED.strategy_selection END,"
            "scan_state=CASE "
            "WHEN banknifty_market_history.scan_state='live_observation' "
            "OR EXCLUDED.scan_state='live_observation' THEN 'live_observation' "
            "WHEN EXCLUDED.market_snapshot ? 'decision_event_key' THEN EXCLUDED.scan_state "
            "ELSE banknifty_market_history.scan_state END",
            (
                state["day"],
                snapshot["spot_at"],
                snapshot["spot"],
                json.dumps(snapshot, default=str),
                json.dumps(selection, default=str),
                state["detail"],
            ),
        )

    def track_plans(self, connection, state, plans, candidates, now, *, minutes=20, reason="eligible_plan"):
        contracts = {c["symbol"]: {k: v for k, v in c.items() if k not in {"quote", "selection_eligible", "tracked"}}
                     for c in candidates}
        for plan in plans:
            contract = contracts.get(plan["symbol"], {"symbol": plan["symbol"], "kind": plan["kind"]})
            connection.execute(
                "INSERT INTO banknifty_tracked_contracts(trading_date,symbol,contract,first_seen_at,last_selected_at,"
                "retain_until,reasons) VALUES(%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb) "
                "ON CONFLICT(trading_date,symbol) DO UPDATE SET contract=EXCLUDED.contract,"
                "last_selected_at=EXCLUDED.last_selected_at,retain_until=GREATEST(banknifty_tracked_contracts.retain_until,"
                "EXCLUDED.retain_until),reasons=CASE WHEN banknifty_tracked_contracts.reasons @> EXCLUDED.reasons "
                "THEN banknifty_tracked_contracts.reasons ELSE banknifty_tracked_contracts.reasons || EXCLUDED.reasons END",
                (state["day"], plan["symbol"], json.dumps(contract), now, now, now + timedelta(minutes=minutes),
                 json.dumps([reason])),
            )

    def tracked_symbols(self, connection, day, now):
        return [row[0] for row in connection.execute(
            "SELECT symbol FROM banknifty_tracked_contracts WHERE trading_date=%s AND retain_until>=%s "
            "ORDER BY last_selected_at DESC LIMIT 64", (day, now)).fetchall()]

    def worker_incident(self, worker, now, status, evidence):
        with self.locked() as connection:
            previous = connection.execute(
                "SELECT status FROM banknifty_worker_health WHERE worker=%s", (worker,)).fetchone()
            if status == "failed" or (status == "recovered" and previous and previous[0] == "failed"):
                connection.execute(
                    "INSERT INTO banknifty_worker_incidents(trading_date,worker,observed_at,status,reason_code,detail) "
                    "VALUES(%s,%s,%s,%s,%s,%s::jsonb)",
                    (now.astimezone(IST).date(), worker, now, status, evidence["reason_code"],
                     json.dumps(evidence, default=str)),
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
            failures = connection.execute(
                "SELECT state,snapshot->>'at' FROM banknifty_ai_calls WHERE trading_date=%s "
                "ORDER BY slot DESC LIMIT 3", (day,)).fetchall()
            if (len(failures) == 3 and all(row[0] == "failed" for row in failures)
                    and failures[0][1] and now < datetime.fromisoformat(failures[0][1]) + timedelta(minutes=15)):
                state["detail"] = "AI circuit open after three failures; retry after 15 minutes; exits remain active"
                self.save(connection, state)
                return None
            used, today = connection.execute(
                "SELECT COALESCE(sum(reserved_usd),0),COALESCE(sum(reserved_usd) "
                "FILTER(WHERE trading_date=%s),0) FROM banknifty_ai_calls WHERE trading_date>=%s",
                (day, now.astimezone(IST).date() - timedelta(days=BUDGET_DAYS - 1)),
            ).fetchone()
            if used + RESERVATION > ROLLING_BUDGET or today + RESERVATION > DAILY_BUDGET:
                state["detail"] = "AI spending limit reached; independent exits remain active"
                self.save(connection, state)
                return None
            call_id = uuid4()
            row = connection.execute(
                "INSERT INTO banknifty_ai_calls(call_id,trading_date,slot,state,reserved_usd,snapshot,created_at) "
                "VALUES(%s,%s,%s,'reserved',%s,%s::jsonb,%s) ON CONFLICT(slot) DO NOTHING RETURNING call_id",
                (call_id, day, int(now.timestamp()) // 120, RESERVATION, json.dumps(snapshot), now),
            ).fetchone()
            return call_id if row else None

    def reconcile_interrupted_calls(self, now):
        """Close abandoned reservations conservatively; never replay a stale decision."""
        with self.locked() as connection:
            return connection.execute(
                "UPDATE banknifty_ai_calls SET state='interrupted', "
                "result=COALESCE(result,'{}'::jsonb) || jsonb_build_object("
                "'recovery',jsonb_build_object('at',%s::text,'reason','process_interrupted','charge_retained',true)) "
                "WHERE state IN ('reserved','completed') AND created_at<%s RETURNING call_id",
                (now.isoformat(), now - timedelta(minutes=10)),
            ).fetchall()

    def settle(self, call_id, decision, usage, failure=None, *, now=None):
        with self.locked() as connection:
            connection.execute(
                "UPDATE banknifty_ai_calls SET state=%s,reserved_usd=%s,result=%s::jsonb WHERE call_id=%s AND state='reserved'",
                (
                    "completed" if decision else "failed",
                    D(usage["budget_charge_usd"]) if usage else (D(0) if failure and not failure["dispatched"] else RESERVATION),
                    json.dumps({"decision": decision, "usage": usage, "failure": failure,
                                "settled_at": (now or datetime.now(IST)).isoformat()}),
                    call_id,
                ),
            )

    def learning_context(self, connection, before_day, state=None):
        rows = connection.execute(
            "SELECT playbook_id,count(*),count(*) FILTER(WHERE pnl>0),sum(pnl),"
            "avg(pnl/nullif(capital,0)*100) FROM banknifty_trade_outcomes "
            "WHERE closed AND NOT recovery AND session_day<%s "
            "AND experiment_id=%s GROUP BY playbook_id",
            (before_day, experiment_id(state)),
        ).fetchall()
        days = connection.execute(
            "SELECT session_day,jsonb_build_object('realized_pnl',sum(pnl)::text,'entries',count(*),'training',false,'final_state','reconciled_flat') "
            "FROM banknifty_trade_outcomes WHERE closed AND NOT recovery AND session_day<%s AND experiment_id=%s "
            "GROUP BY session_day ORDER BY session_day DESC LIMIT 10", (before_day, experiment_id(state))
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
        recovery = connection.execute(
            "SELECT count(*),COALESCE(sum(pnl),0) FROM banknifty_trade_outcomes WHERE session_day=%s AND recovery",
            (state["day"],)).fetchone()
        realized = D(state["realized_pnl"])
        capital = D(state["amount"])
        report = {
            "version": "banknifty-daily-report-v2",
            "recovery_trades": recovery[0], "recovery_pnl": str(recovery[1]),
            "intraday_comparable": recovery[0] == 0 and state["position"] is None,
            "generated_late": str(now.astimezone(IST).date()) != state["day"],
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
            "session_profit_cap_enabled": state.get("session_profit_cap_enabled", True),
            "trade_stop_pct": str(trade_limits(state)[0]),
            "trade_target_pct": str(trade_limits(state)[1]),
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

    def start(self, amount, loss_pct, profit_pct, actor, *, session_profit_cap_enabled=True,
              trade_stop_pct=None, trade_target_pct=None):
        now = self.clock()
        amount, loss, profit = validate_limits(amount, loss_pct, profit_pct)
        _, trade_stop, trade_target = validate_limits(
            amount, loss if trade_stop_pct is None else trade_stop_pct,
            profit if trade_target_pct is None else trade_target_pct)
        if not isinstance(session_profit_cap_enabled, bool):
            raise TypeError("Session profit cap must be a boolean")
        day = str(now.astimezone(IST).date())
        with self.store.locked() as connection:
            old = self.store.latest(connection)
            if old and old["state"] == "completed" and not old["position"]:
                self.store.finalize_learning(connection, old)
            if old and old["day"] == day:
                if (tuple(map(D, (old["amount"], old["loss_pct"], old["profit_pct"]))) != (amount, loss, profit)
                        or trade_limits(old) != (trade_stop, trade_target)
                        or old.get("session_profit_cap_enabled", True) != session_profit_cap_enabled):
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
            if regular_session(now.astimezone(IST).date()) is not True or now.astimezone(IST).time() >= time(15):
                raise ValueError("Start on a verified regular NSE session before 15:00 IST")
            if not self.enabled or not provider_ready() or self.market is None:
                raise ValueError("Bank Nifty AI worker/key/read-only feed is not configured")
            if self.store.halted(connection):
                raise ValueError("Safety halt is active")
            state = {
                "day": day,
                "amount": str(amount),
                "loss_pct": str(loss),
                "profit_pct": str(profit),
                "trade_stop_pct": str(trade_stop),
                "trade_target_pct": str(trade_target),
                "session_profit_cap_enabled": session_profit_cap_enabled,
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
                "version": "banknifty-ai-v6-recovery",
                "provenance": provenance(),
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

    def settle_expired_position(self, position_id, settlement_price, source_reference, actor, *, settlement_fees=0):
        """Record operator-attested expiry evidence, bound to a specific residual."""
        price = D(str(settlement_price))
        costs = D(str(settlement_fees))
        if not price.is_finite() or price < 0:
            raise ValueError("Settlement price must be a non-negative verified amount")
        if not costs.is_finite() or costs < 0:
            raise ValueError("Settlement fees must be non-negative")
        if not isinstance(source_reference, str) or not 3 <= len(source_reference.strip()) <= 500:
            raise ValueError("Settlement evidence reference is required")
        now = self.clock()
        with self.store.locked() as connection:
            previous = connection.execute(
                "SELECT detail FROM banknifty_events WHERE kind='paper_settlement' AND detail->>'position_id'=%s",
                (position_id,),
            ).fetchone()
            if previous:
                detail = previous[0]
                if (D(detail['settlement_price']) != price or D(detail['settlement_fees']) != costs
                        or detail['source_reference'] != source_reference.strip()):
                    raise ValueError("Settlement already recorded with different evidence")
                return {'state': 'already_settled', 'position_id': position_id}
            state = self.store.latest(connection)
            position = state.get("position") if state else None
            if not position or position['id'] != position_id:
                raise ValueError("Requested unresolved paper position no longer matches")
            if position["contract"]["expiry"] >= str(now.astimezone(IST).date()):
                raise ValueError("Settlement is only for an expired contract")
            quantity = position["quantity"]
            proceeds = price * quantity - costs
            entry_cost = D(position.get("entry_cost_remaining", D(position["entry_cost_per_unit"]) * quantity))
            pnl = proceeds - entry_cost
            state["cash"] = str(D(state["cash"]) + proceeds)
            state["realized_pnl"] = str(D(state["realized_pnl"]) + pnl)
            state["pnl"] = state["realized_pnl"]
            state['valuation_fresh'] = True
            state["position"] = None
            state["state"] = "completed"
            state["detail"] = "Expired position reconciled from operator-attested settlement evidence"
            self.store.event(connection, state, "paper_settlement", {
                "position_id": position["id"], "symbol": position["contract"]["symbol"], "quantity": quantity,
                "settlement_price": str(price), "settlement_fees": str(costs), "pnl": str(pnl), "capital": state["amount"],
                "source_reference": source_reference.strip(), "actor": actor, "settled_at": now.isoformat(),
                "expiry": position['contract']['expiry'], "closed": True, "recovery": True,
                "entered_at": position['entered_at'], "evidence_kind": "operator_attestation",
                "playbook_id": position.get("entry_plan", {}).get("playbook_id", "legacy_unattributed"),
                "experiment_id": position.get("experiment_id", "legacy-unbound"),
                "cost_model": "operator_attested_actual",
            })
            self.store.save(connection, state)
            self.store.finalize_learning(connection, state)
            return state

    def status(self):
        with self.store.locked() as connection:
            state = self.store.latest(connection)
            used, rolling, today = connection.execute(
                "SELECT COALESCE(sum(reserved_usd),0),COALESCE(sum(reserved_usd) FILTER(WHERE trading_date>=%s),0),"
                "COALESCE(sum(reserved_usd) FILTER(WHERE trading_date=%s),0) FROM banknifty_ai_calls",
                (self.clock().astimezone(IST).date() - timedelta(days=BUDGET_DAYS - 1), self.clock().astimezone(IST).date()),
            ).fetchone()
            events = connection.execute(
                "SELECT event_at,kind,detail FROM banknifty_events ORDER BY event_id DESC LIMIT 40"
            ).fetchall()
            calls = connection.execute(
                "SELECT created_at,state,result FROM banknifty_ai_calls ORDER BY created_at DESC LIMIT 10"
            ).fetchall()
            review = connection.execute(
                "SELECT playbook_id,experiment_id,count(*) FILTER(WHERE closed), "
                "count(*) FILTER(WHERE closed AND pnl>0),COALESCE(sum(pnl) FILTER(WHERE closed),0),"
                "sum(pnl),count(*) FILTER(WHERE NOT closed) FROM banknifty_trade_outcomes "
                "WHERE NOT recovery GROUP BY playbook_id,experiment_id"
            ).fetchall()
            learning = self.store.learning_context(connection, "9999-12-31", state)
            reports = connection.execute(
                "SELECT trading_date,generated_at,report,delivery_status,delivery_attempts,"
                "delivery_attempted_at,delivery_error FROM banknifty_daily_reports "
                "ORDER BY trading_date DESC LIMIT 10"
            ).fetchall()
            settlements = connection.execute(
                "SELECT trading_date,detail FROM banknifty_events WHERE kind='paper_settlement' ORDER BY event_id"
            ).fetchall()
        return {
            "available": self.enabled and self.market is not None and provider_ready(),
            "execution": "paper-only",
            "operations": diagnostics(self.store, self.clock()),
            "model": MODEL,
            "cost_model": BROKER_COST_VERSION,
            "selector_version": SELECTOR_VERSION,
            "playbooks": catalogue(),
            "paper_review": [
                {
                    "playbook_id": p,
                    "experiment_id": experiment,
                    "closed_trades": n,
                    "winning_trades": w,
                    "closed_net_pnl": str(net),
                    "realized_pnl_including_partial": str(total),
                    "partially_exited_trades": partial,
                }
                for p, experiment, n, w, net, total, partial in review
            ],
            "learning": learning,
            "daily_reports": [
                {
                    **report,
                    "day": str(day),
                    "generated_at": str(generated),
                    "settlement_supplements": [detail for settlement_day, detail in settlements if settlement_day == day],
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
                "rolling_limit_usd": str(ROLLING_BUDGET), "rolling_days": BUDGET_DAYS,
                "rolling_used_or_reserved_usd": str(rolling), "today_used_or_reserved_usd": str(today),
                "daily_limit_usd": str(DAILY_BUDGET),
                "used_or_reserved_usd": str(used),
                "accounting": "conservative estimate, not invoice",
            },
            "events": [{"at": str(at), "kind": kind, "detail": detail} for at, kind, detail in events],
            "decisions": [{"at": str(at), "state": status, "result": result} for at, status, result in calls],
        }

    @property
    def dashboard_url(self):
        return os.getenv("KIWIT_DASHBOARD_URL", "https://kiwit.tathyaforge.in/dashboard")

    def _process_daily_report(self, state, now):
        return report_backlog(self, now)

    def _close(self, connection, state, quote, reason, now):
        position = state["position"]
        if not position or not market_window(now) or not fresh(quote, now):
            return False
        if position["contract"]["expiry"] < str(now.astimezone(IST).date()):
            state["detail"] = "Expired residual position requires verified settlement reconciliation"
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
        proceeds = price * qty - fees(price * qty, "sell")
        remaining_cost = D(
            position.get("entry_cost_remaining", D(position["entry_cost_per_unit"]) * position["quantity"])
        )
        entry_cost = (
            remaining_cost if qty == position["quantity"] else remaining_cost * D(qty) / D(position["quantity"])
        )
        state["cash"] = str(D(state["cash"]) + proceeds)
        realized = (proceeds - entry_cost).quantize(D("0.00000001"))
        state["realized_pnl"] = str(D(state["realized_pnl"]) + realized)
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
                "pnl": str(realized),
                "capital": state["amount"],
                "quote": quote,
                "position_id": position["id"],
                "playbook_id": position.get("entry_plan", {}).get("playbook_id", "legacy_unattributed"),
                "closed": position["quantity"] == 0,
                "entered_at": position["entered_at"], "exited_at": now.isoformat(),
                "holding_seconds": (now - datetime.fromisoformat(position["entered_at"])).total_seconds(),
                "recovery": state["day"] != str(now.astimezone(IST).date()),
                "experiment_id": position.get("experiment_id", "legacy-unbound"),
                "cost_model": BROKER_COST_VERSION,
                "exit_costs": {k: str(v) if isinstance(v, D) else v
                               for k, v in cost_breakdown(price * qty, "sell").items()},
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
                    state["pnl"] = str(D(state["cash"]) + value - fees(value, "sell") - D(state["amount"]))
                    current["mark"] = str(price)
                    current["mark_at"] = quote["stamp"]
                    current["underlying_check"] = underlying
                    pnl = D(state["pnl"])
                    limit_hit = session_limit_reached(state, pnl)
                    if limit_hit:
                        state["detail"] = "Session P&L limit triggered; flattening before shadow-only scans"
                    reason = (
                        "session_stop"
                        if state["state"] == "stopping" or limit_hit
                        else current.get("exit_pending")
                        or (
                            "stop_loss"
                            if price <= D(current["stop"])
                            else "take_profit"
                            if price >= D(current["target"])
                            else "underlying_invalidation"
                            if underlying_exit(current, underlying, now)
                            else "playbook_time_exit"
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
                pnl = D(state["pnl"])
                if state["state"] == "stopping":
                    state["state"] = "completed"
                    state["detail"] = "Session complete; reconciled flat"
                elif session_limit_reached(state, pnl):
                    state["detail"] = "Entry blocked by session P&L limit; shadow scans continue"
                elif state["entries"] >= ENTRY_CAP:
                    state["detail"] = "Entry cap reached; shadow scans continue"
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
            if decision["action"] == "BUY":
                quote = self.market.quote(decision["symbol"], now, entry=True)
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
                    self.store.event(connection, state, "ai_exit_advisory", {
                        "call_id": str(call_id), "position_id": state["position"]["id"],
                        "symbol": decision["symbol"], "summary": decision["summary"],
                        "execution_authority": False,
                    })
                    state["detail"] = "AI exit advisory recorded; deterministic exit rules retain authority"
            elif decision["action"] == "BUY":
                if (
                    state["position"]
                    or not entry_window(now)
                    or self.store.halted(connection)
                    or not self.enabled
                    or state["entries"] >= ENTRY_CAP
                    or decision["strategy"] == "no_trade"
                ):
                    raise ValueError("Current session state blocks entry")
                if state["last_exit"] and (now - datetime.fromisoformat(state["last_exit"])).total_seconds() < 300:
                    raise ValueError("Five-minute cooldown after exit")
                if (
                    not fresh(quote, now)
                    or not 0 <= (now - datetime.fromisoformat(snapshot["spot_at"])).total_seconds() <= 180
                ):
                    raise ValueError("Decision or execution quote is stale")
                pnl = D(state["realized_pnl"])
                if session_limit_reached(state, pnl):
                    raise ValueError("Session P&L limit blocks entry")
                if selected["expiry"] <= state["day"]:
                    raise ValueError("Expiry-day contracts not permitted")
                evidence = entry_evidence(snapshot.get("chart_analysis"), selected["kind"], decision["strategy"], now)
                if not evidence:
                    raise ValueError("No fresh matching chart setup; paper entry blocked")
                plan = validate_plan(decision, snapshot, state, quote, underlying, now)
                qty = plan["quantity"]
                fill = fill_price(quote, selected, True)
                exits = exit_levels(state, plan["live_exit"], fill, qty, selected)
                cost = fill * qty + fees(fill * qty, "buy")
                state["cash"] = str(D(state["cash"]) - cost)
                state["position"] = {
                    "id": str(call_id),
                    "contract": {k: v for k, v in selected.items() if k != "quote"},
                    "quantity": qty,
                    "entry": str(fill),
                    "entry_cost_per_unit": str(cost / qty),
                    "entry_cost_remaining": str(cost),
                    "stop": str(exits["stop"]),
                    "target": str(exits["target"]),
                    "entered_at": now.isoformat(),
                    "exit_deadline": (now + timedelta(minutes=exits["max_hold_minutes"])).isoformat(),
                    "experiment_id": snapshot["experiment_id"],
                    "provenance": snapshot["provenance"],
                    "entry_plan": plan,
                    "entry_underlying": underlying,
                    "exit_policy": "playbook_cost_aware_v5",
                    "cost_aware_exit": {key: str(value) if isinstance(value, D) else value
                                        for key, value in exits.items()},
                    "cost_model": BROKER_COST_VERSION,
                }
                state["entries"] += 1
                self.store.track_plans(connection, state, [plan], snapshot["candidates"], now,
                                       minutes=360, reason="opened_position")
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
                        "entry_costs": {k: str(v) if isinstance(v, D) else v
                                        for k, v in cost_breakdown(fill * qty, "buy").items()},
                    },
                )
            self.store.save(connection, state)

    def observe(self):
        """Read-only market tape; requires no RUN consent and never invokes AI."""
        now = self.clock()
        local = now.astimezone(IST)
        if (not self.market or regular_session(local.date()) is not True
                or not time(9, 20) <= local.time() <= time(15, 30)):
            return {"state": "market_closed"}
        try:
            with self.store.locked() as connection:
                row = connection.execute(
                    "SELECT market_snapshot->'chart_cache' FROM banknifty_market_history "
                    "WHERE trading_date=%s AND scan_state='live_observation' ORDER BY observed_at DESC LIMIT 1",
                    (local.date(),)).fetchone()
                tracked = self.store.tracked_symbols(connection, local.date(), now)
            snapshot = (self.market.snapshot(now, cached_context=row[0] if row else None,
                                             tracked_symbols=tracked)
                        if isinstance(self.market, BankNiftyMarket) else self.market.snapshot(now))
            snapshot["provenance"] = provenance()
            with self.store.locked() as connection:
                self.store.record_market_snapshot(connection,
                    {"day": str(local.date()), "detail": "live_observation"}, snapshot,
                    {"version": SELECTOR_VERSION, "plans": [], "evaluations": [], "mode": "observation_only"})
            self.store.worker_incident("observer", self.clock(), "recovered",
                                       {"reason_code": "MARKET_SNAPSHOT_RECOVERED", "spot_at": snapshot["spot_at"]})
            heartbeat(self.store, "observer", self.clock(), "ok", {
                "spot_at": snapshot["spot_at"], "tracked_contracts": len(tracked),
                "feature_coverage": snapshot.get("option_feature_coverage", {}),
            })
            return {"state": "observed"}
        except (OSError, ValueError, ArithmeticError, BrokerApiError) as error:
            evidence = market_failure_evidence(error)
            self.store.worker_incident("observer", self.clock(), "failed", evidence)
            heartbeat(self.store, "observer", self.clock(), "failed", evidence)
            return {"state": "unavailable", "reason_code": evidence["reason_code"]}

    def observed_snapshot(self, now):
        with self.store.locked() as connection:
            row = connection.execute(
                "SELECT market_snapshot FROM banknifty_market_history WHERE trading_date=%s "
                "AND scan_state='live_observation' ORDER BY observed_at DESC LIMIT 1",
                (now.astimezone(IST).date(),)).fetchone()
        if not row or not 0 <= (now-datetime.fromisoformat(row[0]["spot_at"])).total_seconds() <= 180:
            raise ValueError("Independent observer snapshot missing or stale")
        return row[0]

    def supervise(self):
        try:
            state = self._monitor()
            heartbeat(self.store, "supervisor", self.clock(), "ok")
            return state
        except Exception:
            heartbeat(self.store, "supervisor", self.clock(), "failed")
            raise

    def run_once(self):
        self.store.reconcile_interrupted_calls(self.clock())
        state = self._monitor()  # Only the dedicated supervisor publishes its own heartbeat.
        now = self.clock()
        self._process_daily_report(state, now)
        if not state or state["state"] != "running" or not self.enabled or not entry_window(now):
            heartbeat(self.store, "decision", now, "idle", {"state": state["state"] if state else "idle"})
            return {"state": state["state"] if state else "idle", "execution": "paper-only"}
        call_id = None
        try:
            snapshot = (
                self.observed_snapshot(now)
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
                    experiment_id=experiment_id(current), provenance=provenance(),
                    day=current["day"],
                    history=current["history"],
                    position=current["position"],
                    capital=current["amount"],
                    loss_pct=current["loss_pct"],
                    profit_pct=current["profit_pct"],
                    session_profit_cap_enabled=current.get("session_profit_cap_enabled", True),
                    trade_stop_pct=str(trade_limits(current)[0]),
                    trade_target_pct=str(trade_limits(current)[1]),
                    cash=current["cash"],
                    realized_pnl=current["realized_pnl"],
                    entries=current["entries"],
                )
                snapshot["event_context"] = event_context(now)
                snapshot["learning_context"] = self.store.learning_context(connection, current["day"], current)
                selection = select_plans(snapshot, current, now)
                current["strategy_selection"] = selection
                snapshot["strategy_selection"] = selection
                tracked_for_prompt = {plan["symbol"] for plan in selection["plans"]}
                if current["position"]:
                    tracked_for_prompt.add(current["position"]["contract"]["symbol"])
                premium_rows = connection.execute(
                    "SELECT observed_at,market_snapshot->'candidates' FROM banknifty_market_history "
                    "WHERE trading_date=%s AND scan_state='live_observation' ORDER BY observed_at DESC LIMIT 12",
                    (current["day"],),
                ).fetchall()
                premium_history = []
                for observed_at, contracts in reversed(premium_rows):
                    for contract in contracts or []:
                        if contract.get("symbol") not in tracked_for_prompt:
                            continue
                        quote = contract.get("quote") or {}
                        premium_history.append({
                            "at": observed_at.isoformat(), "symbol": contract["symbol"],
                            "bid": quote.get("bid"), "ask": quote.get("ask"),
                            "market_fields": quote.get("market_fields") or {},
                        })
                snapshot["premium_history"] = premium_history[-40:]
                gate = entry_gate(current, now, selection["plans"], snapshot["event_context"])
                snapshot["entry_gate"] = gate
                trigger = decision_event(snapshot)
                snapshot["decision_event_key"] = trigger["key"]
                previous = connection.execute(
                    "SELECT snapshot->>'decision_event_key',state,created_at FROM banknifty_ai_calls WHERE trading_date=%s "
                    "AND snapshot->>'decision_event_key' IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                    (current["day"],),
                ).fetchone()
                retry_due = bool(previous and previous[1] in {"failed", "interrupted"}
                                 and now >= previous[2] + timedelta(minutes=2))
                trigger["new"] = bool(trigger["call"] and
                                      (not previous or previous[0] != trigger["key"] or retry_due))
                if selection.get("shadow_plans") and not current["position"] and not gate["allowed"]:
                    current["detail"] = "Entry blocked; shadow scan retained: " + ", ".join(gate["reason_codes"])
                elif not selection["plans"] and not current["position"]:
                    current["detail"] = "No eligible entry plan; waiting for a supported setup"
                self.store.save(connection, current)
                self.store.record_market_snapshot(connection, current, snapshot, selection)
                self.store.track_plans(connection, current, selection["plans"], snapshot["candidates"], now)
                self.store.track_plans(connection, current, selection.get("shadow_plans", []),
                                       snapshot["candidates"], now, minutes=60,
                                       reason="calendar_blocked_shadow")
                self.store.event(connection, current, "strategy_scan", {
                    **selection, "entry_gate": gate, "decision_event": trigger,
                    "mode": "executable" if gate["allowed"] else "shadow",
                })
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
                heartbeat(self.store, "decision", self.clock(), "ok", {"state": "warming_up"})
                return {"state": "warming_up"}
            if analysis and not analysis["ready"]:
                raise ValueError("Chart context incomplete: " + "; ".join(analysis["issues"]))
            if not snapshot["candidates"]:
                raise ValueError("No fresh liquid Bank Nifty option candidates")
            if not snapshot["position"] and not gate["allowed"]:
                heartbeat(self.store, "decision", self.clock(), "ok", {
                    "state": "shadow_scan", "reason_codes": gate["reason_codes"], "ai_called": False})
                return {"state": "shadow_scan", "reason_codes": gate["reason_codes"], "ai_called": False}
            if not trigger["new"]:
                heartbeat(self.store, "decision", self.clock(), "ok", {
                    "state": "waiting_for_material_change", "event": trigger["kind"], "ai_called": False})
                return {"state": "waiting_for_material_change", "ai_called": False}
            times = [datetime.fromisoformat(item["at"]) for item in snapshot["history"][-5:]]
            if any(not 0 < (b - a).total_seconds() <= 120 for a, b in itertools.pairwise(times)):
                raise ValueError("Underlying history has gaps")
            if hasattr(self.analyst, "prepare"):
                body = self.analyst.prepare(snapshot)
                # Body hash is written alongside the reserved input, not added to the prompt recursively.
                request_hash = hashlib.sha256(body).hexdigest()
            else:
                request_hash = None
            call_id = self.store.reserve(self.clock(), snapshot)
            if call_id:
                try:
                    decision, usage = self.analyst.decide(snapshot)
                except (OSError, TimeoutError, ValueError, ArithmeticError) as error:
                    failure = failure_evidence(error)
                    failure["request_sha256"] = request_hash
                    self.store.settle(call_id, None, None, failure, now=self.clock())
                    typed = AIFailure(failure["category"], dispatched=failure["dispatched"])
                    typed.evidence = failure
                    raise typed from None
                self.store.settle(call_id, decision, usage, now=self.clock())
                self._apply(decision, snapshot, call_id)
            heartbeat(self.store, "decision", self.clock(), "ok", {"ai_called": bool(call_id), "scan_at": now.isoformat()})
            return {"state": "running", "ai_called": bool(call_id)}
        except (BrokerApiError, OSError, TimeoutError, ValueError, ArithmeticError) as error:
            is_ai_failure = bool(call_id and isinstance(error, AIFailure))
            with self.store.locked() as connection:
                current = self.store.latest(connection)
                detail = str(error) if isinstance(error, (ValueError, AIFailure)) else "Market data or AI unavailable"
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
            if is_ai_failure:
                category = (getattr(error, "evidence", None) or {}).get("category") or "unspecified"
                delivery, _delivery_error = self.mailer.send_ai_failure(
                    occurred_at=self.clock(),
                    call_id=str(call_id),
                    dashboard_url=os.getenv("KIWIT_DASHBOARD_URL", "https://kiwit.tathyaforge.in/dashboard"),
                    category=category,
                )
                with self.store.locked() as connection:
                    connection.execute("UPDATE banknifty_ai_calls SET result=jsonb_set(result,'{alert_delivery}',%s::jsonb) "
                                       "WHERE call_id=%s", (json.dumps(delivery), call_id))
                heartbeat(self.store, "decision", self.clock(), "failed", {"reason": category})
            elif isinstance(error, AIFailure):
                category = (getattr(error, "evidence", None) or {}).get("category") or "request_validation"
                heartbeat(self.store, "decision", self.clock(), "ok",
                          {"reason": category, "ai_called": False, "scan_at": now.isoformat()})
            else:
                heartbeat(self.store, "decision", self.clock(), "ok",
                          {"reason": "entry_rejected", "ai_called": bool(call_id), "scan_at": now.isoformat()})
            return {"state": "blocked", "detail": detail}
