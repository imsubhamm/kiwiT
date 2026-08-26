"""Day-scoped consent for experimental simulation, never broker execution or promotion."""

from __future__ import annotations

from contextlib import contextmanager
from copy import copy
from datetime import UTC, datetime, time
from decimal import ROUND_DOWN, Decimal
from functools import wraps
from uuid import uuid4
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
ENTRY_CUTOFF = time(15, 0)
FLATTEN_TIME = time(15, 10)  # conservative cash-market cutoff; remainder is reconciliation
MAX_ENTRIES = 10
COST_RATE = Decimal("0.001")  # illustrative 10bps/side, not a broker fee quotation


class BoundDatabase:
    def __init__(self, connection):
        self.connection = connection

    @contextmanager
    def transaction(self):
        with self.connection.transaction():
            yield self.connection

    @contextmanager
    def connect(self, **kwargs):
        yield self.connection


def serialized(method):
    """Serialize worker, manual approvals and run/stop for one account across processes."""

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        if isinstance(self.database, BoundDatabase):
            return method(self, *args, **kwargs)
        with self.database.transaction() as connection:
            connection.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))", ("paper-session:" + self.settings.account_id,)
            )
            scoped = copy(self)
            scoped.database = BoundDatabase(connection)
            return method(scoped, *args, **kwargs)

    return wrapped


def validate_limits(amount, loss_pct, profit_pct):
    values = tuple(Decimal(str(x)) for x in (amount, loss_pct, profit_pct))
    if not all(x.is_finite() and x > 0 for x in values):
        raise ValueError("Amount and percentages must be finite and positive")
    if values[0] > 1_000_000 or values[1] > 25 or values[2] > 100:
        raise ValueError("Paper limits: amount ≤ ₹10,00,000, loss ≤ 25%, profit ≤ 100%")
    if values[0] != values[0].quantize(Decimal(".01")):
        raise ValueError("Amount supports two decimal places")
    if any(x != x.quantize(Decimal(".0001")) for x in values[1:]):
        raise ValueError("Percentages support four decimal places")
    return values


def session_quantity(amount, pnl, exposure, cash, fill, loss_pct):
    """No leverage; per-position risk ≤0.5% of budget and allocation ≤25%."""
    available = max(Decimal(0), min(amount + min(pnl, Decimal(0)) - exposure, cash, amount / 4))
    cost = fill * (1 + COST_RATE)
    risk_per_unit = fill * (loss_pct / 100 + 2 * COST_RATE)
    return max(
        0, int(min(available / cost, amount * Decimal(".005") / risk_per_unit).to_integral_value(rounding=ROUND_DOWN))
    )


class PaperSessionMixin:
    def session_status(self):
        with self.database.connect(autocommit=True) as connection:
            row = connection.execute(
                "SELECT session_id,trading_date,amount,loss_pct,profit_pct,state,approved_by,approved_at,detail "
                "FROM paper_sessions WHERE account_id=%s ORDER BY trading_date DESC LIMIT 1",
                (self.settings.account_id,),
            ).fetchone()
            if not row:
                return None
            pnl, exposure, entries, open_count, fresh = self._session_totals(connection, row[0], datetime.now(UTC))
        return dict(
            zip(
                (
                    "session_id",
                    "trading_date",
                    "amount",
                    "loss_pct",
                    "profit_pct",
                    "state",
                    "approved_by",
                    "approved_at",
                    "detail",
                ),
                map(str, row),
                strict=True,
            ),
            pnl=str(pnl),
            exposure=str(exposure),
            entries=entries,
            open_positions=open_count,
            valuation_fresh=fresh,
            execution="paper-only",
            experimental=True,
        )

    def _active_session(self, connection):
        return connection.execute(
            "SELECT session_id,trading_date,amount,loss_pct,profit_pct,state,approved_at FROM paper_sessions "
            "WHERE account_id=%s AND state IN ('armed','running','stopping') FOR UPDATE",
            (self.settings.account_id,),
        ).fetchone()

    def _session_totals(self, connection, session_id, now):
        rows = connection.execute(
            "SELECT s.status,s.quantity,s.entry_fill_price,s.realized_pnl,q.bid_price,q.observed_at "
            "FROM intraday_signals s LEFT JOIN LATERAL (SELECT bid_price,observed_at FROM intraday_quotes "
            "WHERE symbol=s.symbol AND exchange='NSE' ORDER BY observed_at DESC LIMIT 1) q ON true "
            "WHERE s.session_id=%s AND s.status IN ('entered','exited')",
            (session_id,),
        ).fetchall()
        pnl = exposure = Decimal(0)
        opened = 0
        fresh = True
        for status, qty, entry, realized, bid, stamp in rows:
            if status == "exited":
                pnl += realized or Decimal(0)
            else:
                opened += 1
                exposure += entry * qty * (1 + COST_RATE)
                fresh = fresh and stamp is not None and 0 <= (now - stamp).total_seconds() <= 120
                mark = Decimal(bid) / Decimal("1.0005") if bid else entry
                pnl += (mark * (1 - COST_RATE) - entry * (1 + COST_RATE)) * qty
        return pnl, exposure, len(rows), opened, fresh

    @serialized
    def start_session(self, amount, loss_pct, profit_pct, actor, now=None):
        now = now or datetime.now(UTC)
        amount, loss_pct, profit_pct = validate_limits(amount, loss_pct, profit_pct)
        local = now.astimezone(IST)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT amount,loss_pct,profit_pct FROM paper_sessions WHERE account_id=%s AND trading_date=%s",
                (self.settings.account_id, local.date()),
            ).fetchone()
            if existing:
                if tuple(existing) != (amount, loss_pct, profit_pct):
                    raise ValueError("Today’s run is immutable. Limits cannot be changed or reset mid-day.")
                return self.session_status()  # duplicate Run is idempotent, never resets loss budget
            if local.weekday() >= 5 or local.time() >= ENTRY_CUTOFF:
                raise ValueError("Run is available on weekdays before 15:00 IST; try the next trading day")
            if not self.settings.enabled or self.broker is None:
                raise ValueError("Market-data worker is disabled or Groww read-only access is unconfigured")
            connection.execute("LOCK TABLE system_halts IN SHARE MODE")
            if connection.execute(
                "SELECT EXISTS(SELECT 1 FROM system_halts WHERE active AND scope IN ('global',%s))",
                (self.settings.account_id,),
            ).fetchone()[0]:
                raise ValueError("Safety halt is active; Run cannot override it")
            if self._active_session(connection):
                raise ValueError("A previous session still needs reconciliation; no new run allowed")
            if connection.execute(
                "SELECT EXISTS(SELECT 1 FROM paper_positions WHERE account_id=%s AND quantity<>0)",
                (self.settings.account_id,),
            ).fetchone()[0]:
                raise ValueError("Existing positions must be reconciled before starting a session")
            account = connection.execute(
                "SELECT cash_balance,status FROM paper_accounts WHERE account_id=%s FOR UPDATE",
                (self.settings.account_id,),
            ).fetchone()
            if not account or account[1] != "active" or account[0] < amount:
                raise ValueError("Amount exceeds available simulated cash or account is inactive")
            session_id = uuid4()
            connection.execute(
                "INSERT INTO paper_sessions(session_id,account_id,trading_date,amount,loss_pct,profit_pct,state,"
                "approved_by,approved_at,updated_at,detail) VALUES(%s,%s,%s,%s,%s,%s,'armed',%s,%s,%s,%s)",
                (
                    session_id,
                    self.settings.account_id,
                    local.date(),
                    amount,
                    loss_pct,
                    profit_pct,
                    actor,
                    now,
                    now,
                    "Run approved. Waiting for market window, fresh quotes and a matching experimental setup.",
                ),
            )
            connection.execute(
                "UPDATE intraday_signals SET status='expired',updated_at=%s WHERE account_id=%s AND status='pending'",
                (now, self.settings.account_id),
            )
            self._audit(
                connection,
                "paper_session_approved",
                actor,
                {
                    "session_id": str(session_id),
                    "amount": str(amount),
                    "loss_pct": str(loss_pct),
                    "profit_pct": str(profit_pct),
                    "router": "observer-session-v1",
                    "experimental": True,
                    "scope": "one day; automatic paper strategy selection, entries and exits; no live orders",
                },
            )
        return self.session_status()

    @serialized
    def stop_session(self, actor, now=None):
        now = now or datetime.now(UTC)
        with self.database.transaction() as connection:
            session = self._active_session(connection)
            if session:
                self._set_session(
                    connection, session[0], "stopping", "Operator stop: awaiting fresh executable quotes to exit", now
                )
                self._audit(connection, "paper_session_stop_requested", actor, {"session_id": str(session[0])})
        return self.session_status()

    def _set_session(self, connection, session_id, state, detail, now):
        connection.execute(
            "UPDATE paper_sessions SET state=%s,detail=%s,updated_at=%s WHERE session_id=%s",
            (state, detail, now, session_id),
        )

    def session_tick(self, now):
        """Called inside the serialized worker transaction, before and after signals."""
        with self.database.transaction() as connection:
            session = self._active_session(connection)
            if not session:
                return None
            sid, day, amount, loss, profit, state, _approved_at = session
            pnl, _exposure, entries, _opened, fresh = self._session_totals(connection, sid, now)
            local = now.astimezone(IST)
            reason = None
            if state == "stopping":
                reason = "session_stop"
            elif day != local.date() or local.time() >= FLATTEN_TIME:
                reason = "session_end_of_day"
            elif pnl <= -amount * loss / 100:
                reason = "session_loss_limit"
            elif pnl >= amount * profit / 100:
                reason = "session_profit_target"
            elif entries >= MAX_ENTRIES:
                reason = "session_trade_limit"
            if reason:
                self._set_session(connection, sid, "stopping", reason + ": closing paper positions", now)
                self.monitor_exits(now, force_reason=reason, session_id=sid)
                remaining = self._session_totals(connection, sid, now)[3]
                self._set_session(
                    connection,
                    sid,
                    "stopping" if remaining else "completed",
                    reason
                    + (": waiting for fresh market quotes; positions remain open" if remaining else ": finished"),
                    now,
                )
                connection.execute(
                    "UPDATE intraday_signals SET status='expired',updated_at=%s WHERE session_id=%s AND status='pending'",
                    (now, sid),
                )
                if not remaining:
                    self._audit(
                        connection,
                        "paper_session_completed",
                        "kiwit-worker",
                        {"session_id": str(sid), "reason": reason},
                    )
                return None
            if not fresh:
                self._set_session(connection, sid, "running", "Paused: an open position has a stale quote", now)
                return None
            if local.time() < time(9, 30) or local.time() >= ENTRY_CUTOFF:
                self._set_session(
                    connection,
                    sid,
                    "armed" if local.time() < time(9, 30) else "running",
                    "Waiting for 09:30 IST" if local.time() < time(9, 30) else "Entries closed; monitoring exits",
                    now,
                )
                return None
            pending = connection.execute(
                "SELECT signal_id FROM intraday_signals WHERE session_id=%s AND status='pending' ORDER BY signal_at",
                (sid,),
            ).fetchall()
            detail = (
                "Monitoring open paper positions; stops and session limits checked every minute."
                if _opened
                else "Scanning: no fresh matching setup yet (20 same-day quotes required). No forced trades."
            )
            for (signal_id,) in pending:
                try:
                    self.review(signal_id, True, "paper-session:" + str(sid), "Authorized by Run", now, session_id=sid)
                    detail = "Automatic paper entry recorded; monitoring stops and session limits"
                except ValueError as error:
                    detail = "Waiting: " + str(error)
            self._set_session(connection, sid, "running", detail, now)
            return session

    def session_entry_terms(self, connection, signal_id, session_id, fill, now, cash):
        session = self._active_session(connection)
        if not session_id:
            if (
                session
                or connection.execute(
                    "SELECT session_id FROM intraday_signals WHERE signal_id=%s AND session_id IS NOT NULL",
                    (signal_id,),
                ).fetchone()
            ):
                raise ValueError("Run owns these decisions; manual entry is disabled for session signals")
            return None
        if not session or session[0] != session_id or session[5] not in ("armed", "running"):
            raise ValueError("Session is no longer authorized for entries")
        sid, day, amount, loss, profit, _state, approved_at = session
        local = now.astimezone(IST)
        if local.weekday() >= 5 or local.date() != day or not time(9, 30) <= local.time() < ENTRY_CUTOFF:
            raise ValueError("Outside session entry window")
        linked = connection.execute(
            "SELECT signal_id FROM intraday_signals WHERE signal_id=%s AND session_id=%s AND signal_at>=%s",
            (signal_id, sid, approved_at),
        ).fetchone()
        if not linked:
            raise ValueError("Signal was not created under this run approval")
        pnl, exposure, entries, _opened, fresh = self._session_totals(connection, sid, now)
        if not fresh or pnl <= -amount * loss / 100 or pnl >= amount * profit / 100 or entries >= MAX_ENTRIES:
            raise ValueError("Session risk, freshness or trade limit blocks entry")
        quantity = session_quantity(amount, pnl, exposure, cash, fill, loss)
        if quantity < 1:
            raise ValueError("Budget too small for one unit within the risk limits")
        return quantity, loss / 100, profit / 100
