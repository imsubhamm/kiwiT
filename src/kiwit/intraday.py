from __future__ import annotations

import json
import os
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from email.message import EmailMessage
from typing import Any
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from .brokers.groww import GrowwBrokerClient
from .database import PostgresDatabase
from .paper_session import COST_RATE, ENTRY_CUTOFF, FLATTEN_TIME, PaperSessionMixin, serialized

IST = ZoneInfo("Asia/Kolkata")
ACCOUNT_ID = "kiwit-paper-main"
STRATEGY_ID = "intraday_regime_observer"
STRATEGY_VERSION = "1.0.0-paper"
FRESHNESS_SECONDS = 120


def _decimal(value: Any, default: Decimal | None = None) -> Decimal:
    try:
        result = Decimal(str(value))
        if result.is_finite() and result > 0:
            return result
    except (TypeError, ValueError, ArithmeticError):
        pass
    if default is None:
        raise ValueError("quote price is missing")
    return default


def _quote_time(payload: dict[str, Any], now: datetime) -> datetime:
    value = payload.get("timestamp") or payload.get("last_trade_time") or payload.get("lastTradeTime")
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, (int, float)):
        if value > 10_000_000_000:
            value /= 1000
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST).astimezone(UTC)
        except ValueError:
            pass
    raise ValueError("quote has no usable exchange timestamp; freshness cannot be verified")


@dataclass(frozen=True)
class IntradaySettings:
    symbols: tuple[str, ...] = ("NIFTYBEES",)
    account_id: str = ACCOUNT_ID
    stop_fraction: Decimal = Decimal("0.05")
    target_fraction: Decimal = Decimal("0.10")
    risk_fraction: Decimal = Decimal("0.005")
    max_notional_fraction: Decimal = Decimal("0.10")
    enabled: bool = True

    @classmethod
    def from_env(cls) -> IntradaySettings:
        symbols = tuple(
            symbol.strip().upper() for symbol in os.getenv("KIWIT_INTRADAY_SYMBOLS", "NIFTYBEES").split(",")
            if symbol.strip()
        )
        return cls(
            symbols=symbols,
            account_id=os.getenv("KIWIT_PAPER_ACCOUNT_ID", ACCOUNT_ID),
            stop_fraction=Decimal(os.getenv("KIWIT_INTRADAY_STOP_FRACTION", "0.05")),
            target_fraction=Decimal(os.getenv("KIWIT_INTRADAY_TARGET_FRACTION", "0.10")),
            risk_fraction=Decimal(os.getenv("KIWIT_INTRADAY_RISK_FRACTION", "0.005")),
            max_notional_fraction=Decimal(os.getenv("KIWIT_INTRADAY_MAX_NOTIONAL_FRACTION", "0.10")),
            enabled=os.getenv("KIWIT_INTRADAY_ENABLED", "true").lower() == "true",
        )


class SignalMailer:
    def __init__(self) -> None:
        self.host = os.getenv("KIWIT_SMTP_HOST", "")
        self.port = int(os.getenv("KIWIT_SMTP_PORT", "465"))
        self.implicit_tls = os.getenv("KIWIT_SMTP_IMPLICIT_TLS", "true").lower() == "true"
        self.username = os.getenv("KIWIT_SMTP_USERNAME", "")
        self.password = os.getenv("KIWIT_SMTP_PASSWORD", "")
        self.sender = os.getenv("KIWIT_EMAIL_FROM", self.username)
        self.recipient = os.getenv("KIWIT_ALERT_EMAIL", os.getenv("KIWIT_ADMIN_EMAIL", ""))

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender and self.recipient)

    def send_signal(self, signal: dict[str, Any], dashboard_url: str) -> tuple[str, str]:
        if not self.configured:
            return "not_configured", "SMTP environment variables are not configured"
        message = EmailMessage()
        message["Subject"] = f"kiwiT paper signal: {signal['symbol']} {signal['pattern']}"
        message["From"] = self.sender
        message["To"] = self.recipient
        message.set_content(
            "A new paper-only signal is waiting for review.\n\n"
            f"Symbol: {signal['symbol']}\nRegime: {signal['regime']}\nPattern: {signal['pattern']}\n"
            f"Entry: {signal['entry_price']}\nStop: {signal['stop_price']}\nTarget: {signal['target_price']}\n"
            f"Quantity: {signal['quantity']}\nExpires: {signal['expires_at']}\n\n"
            f"Review it at {dashboard_url}\n\nNo live broker order will be placed."
        )
        try:
            client_type = smtplib.SMTP_SSL if self.implicit_tls else smtplib.SMTP
            with client_type(self.host, self.port, timeout=10) as client:
                if not self.implicit_tls:
                    client.starttls()
                if self.username:
                    client.login(self.username, self.password)
                client.send_message(message)
            return "sent", ""
        except (OSError, smtplib.SMTPException) as error:
            return "failed", f"{type(error).__name__}: email delivery failed"


class IntradayService(PaperSessionMixin):
    """Persistent, human-approved paper workflow. It has no live-order code path."""

    def __init__(
        self, database: PostgresDatabase, broker: GrowwBrokerClient | None,
        settings: IntradaySettings | None = None, mailer: SignalMailer | None = None,
    ) -> None:
        self.database = database
        self.broker = broker
        self.settings = settings or IntradaySettings.from_env()
        self.mailer = mailer or SignalMailer()

    def _audit(
        self, connection: Any, event_type: str, actor: str, details: dict[str, Any], signal_id: UUID | None = None,
    ) -> None:
        connection.execute(
            "INSERT INTO intraday_audit_events(event_id,signal_id,account_id,event_type,actor,event_at,details) "
            "VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb)",
            (uuid4(), signal_id, self.settings.account_id, event_type, actor, datetime.now(UTC), json.dumps(details)),
        )

    def ingest_quote(self, symbol: str, now: datetime) -> dict[str, Any]:
        if self.broker is None:
            raise RuntimeError("Groww read-only market data is not configured")
        payload = self.broker.quote(symbol)
        last = _decimal(payload.get("last_price") or payload.get("ltp") or payload.get("lastPrice"))
        bid = _decimal(payload.get("bid_price") or payload.get("bid") or payload.get("best_bid_price"), last)
        ask = _decimal(payload.get("offer_price") or payload.get("ask_price") or payload.get("ask") or payload.get("best_ask_price"), last)
        observed_at = _quote_time(payload, now)
        if not 0 <= (now - observed_at).total_seconds() <= FRESHNESS_SECONDS:
            raise ValueError("Groww quote is stale or future-dated")
        if ask < bid:
            bid = ask = last
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO intraday_quotes(symbol,exchange,observed_at,last_price,bid_price,ask_price,source,source_payload) "
                "VALUES(%s,'NSE',%s,%s,%s,%s,'groww-read-only',%s::jsonb) ON CONFLICT DO NOTHING",
                (symbol, observed_at, last, bid, ask, json.dumps(payload, default=str)),
            )
        return {"symbol": symbol, "observed_at": observed_at, "last": last, "bid": bid, "ask": ask}

    def freshness(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        with self.database.connect(autocommit=True) as connection:
            rows = connection.execute(
                "SELECT DISTINCT ON(symbol) symbol,observed_at,ingested_at,last_price,source "
                "FROM intraday_quotes WHERE symbol=ANY(%s) ORDER BY symbol,observed_at DESC",
                (list(self.settings.symbols),),
            ).fetchall()
            worker = connection.execute(
                "SELECT state,started_at,completed_at,detail FROM intraday_worker_runs ORDER BY started_at DESC LIMIT 1"
            ).fetchone()
        by_symbol = {row[0]: row for row in rows}
        instruments = []
        for symbol in self.settings.symbols:
            row = by_symbol.get(symbol)
            age = int((now - row[1]).total_seconds()) if row else None
            instruments.append({
                "symbol": symbol, "observed_at": row[1].isoformat() if row else None,
                "last_price": str(row[3]) if row else None, "source": row[4] if row else None,
                "age_seconds": age, "fresh": age is not None and 0 <= age <= FRESHNESS_SECONDS,
            })
        return {
            "market_window": self._window_state(now), "freshness_limit_seconds": FRESHNESS_SECONDS,
            "instruments": instruments,
            "worker": ({"state": worker[0], "started_at": worker[1].isoformat(),
                        "completed_at": worker[2].isoformat() if worker[2] else None, "detail": worker[3]} if worker else None),
        }

    @staticmethod
    def _window_state(now: datetime) -> str:
        local = now.astimezone(IST)
        if local.weekday() >= 5:
            return "closed"
        if time(9, 30) <= local.time() <= time(15, 30):
            return "entry_window"
        if time(15, 30) < local.time() <= time(15, 45):
            return "reconciliation_window"
        return "closed"

    def _create_signal(self, symbol: str, now: datetime) -> UUID | None:
        with self.database.transaction() as connection:
            session = self._active_session(connection)
            if not session and connection.execute('SELECT 1 FROM paper_sessions WHERE account_id=%s AND trading_date=%s',
                                                 (self.settings.account_id,now.astimezone(IST).date())).fetchone():
                return None  # completing a run must not fall back into the manual observer
            if session and (session[5] == 'stopping' or session[1] != now.astimezone(IST).date()):
                return None
            if now.astimezone(IST).time() >= ENTRY_CUTOFF:
                return None
            if session and connection.execute(
                "SELECT 1 FROM intraday_signals WHERE session_id=%s AND symbol=%s "
                "AND (status IN ('pending','entered') OR exit_filled_at>%s) LIMIT 1",
                (session[0],symbol,now-timedelta(minutes=5)),
            ).fetchone():
                return None
            rows = connection.execute(
                "SELECT observed_at,last_price FROM intraday_quotes WHERE symbol=%s AND exchange='NSE' "
                "AND observed_at>=%s AND observed_at<=%s ORDER BY observed_at DESC LIMIT 30",
                (symbol, datetime.combine(now.astimezone(IST).date(), time(9,30), IST), now),
            ).fetchall()
            if len(rows) < 20 or (now - rows[0][0]).total_seconds() > FRESHNESS_SECONDS:
                return None
            if any((rows[i][0]-rows[i+1][0]).total_seconds() > 120 for i in range(19)):
                return None
            prices = [Decimal(row[1]) for row in reversed(rows)]
            current = prices[-1]
            fast = sum(prices[-5:]) / Decimal(5)
            slow = sum(prices[-20:]) / Decimal(20)
            previous_high = max(prices[-11:-1])
            changes = [prices[index] - prices[index - 1] for index in range(1, len(prices))]
            gains = sum((change for change in changes[-2:] if change > 0), Decimal(0))
            losses = abs(sum((change for change in changes[-2:] if change < 0), Decimal(0)))
            rsi2 = Decimal(100) if losses == 0 else Decimal(100) - Decimal(100) / (Decimal(1) + gains / losses)
            if current > previous_high and fast > slow:
                regime, pattern = "bull_trend", "10-bar breakout"
            elif abs(fast / slow - Decimal(1)) <= Decimal("0.003") and rsi2 < Decimal(15):
                regime, pattern = "range", "RSI(2) pullback"
            else:
                return None
            account = connection.execute(
                "SELECT cash_balance FROM paper_accounts WHERE account_id=%s FOR UPDATE", (self.settings.account_id,),
            ).fetchone()
            if not account:
                raise RuntimeError("paper account does not exist")
            risk_per_share = current * self.settings.stop_fraction
            risk_quantity = int((Decimal(account[0]) * self.settings.risk_fraction) / risk_per_share)
            notional_quantity = int((Decimal(account[0]) * self.settings.max_notional_fraction) / current)
            quantity = min(risk_quantity, notional_quantity)
            if quantity < 1:
                return None
            signal_id = uuid4()
            expires = min(now + timedelta(minutes=10), datetime.combine(now.astimezone(IST).date(), time(15, 15), IST))
            stop = current * (Decimal(1) - self.settings.stop_fraction)
            target = current * (Decimal(1) + self.settings.target_fraction)
            rationale = {
                "fast_sma": str(fast), "slow_sma": str(slow), "rsi2": str(rsi2),
                "stop_fraction": str(self.settings.stop_fraction), "target_fraction": str(self.settings.target_fraction),
                "paper_only": True,
                "experimental": True,
                "session_id": str(session[0]) if session else None,
            }
            inserted = connection.execute(
                "INSERT INTO intraday_signals(signal_id,account_id,strategy_id,strategy_version,symbol,regime,pattern,side,"
                "signal_at,expires_at,entry_price,stop_price,target_price,quantity,rationale,status,session_id) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,'buy',%s,%s,%s,%s,%s,%s,%s::jsonb,'pending',%s) "
                "ON CONFLICT DO NOTHING RETURNING signal_id",
                (signal_id, self.settings.account_id, STRATEGY_ID, STRATEGY_VERSION, symbol, regime, pattern, now, expires,
                 current, stop, target, quantity, json.dumps(rationale), session[0] if session else None),
            ).fetchone()
            if not inserted:
                return None
            self._audit(connection, "signal_created", "kiwit-worker", rationale, signal_id)
        # Run sessions require no per-signal email or human approval; UI/audit are the activity log.
        if session:
            return signal_id
        signal = self.get_signal(signal_id)
        dashboard = os.getenv("KIWIT_DASHBOARD_URL", "/dashboard")
        delivery_status, error = self.mailer.send_signal(signal, dashboard)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO notification_deliveries(delivery_id,signal_id,channel,recipient,status,attempted_at,error_message) "
                "VALUES(%s,%s,'email',%s,%s,%s,%s)",
                (uuid4(), signal_id, self.mailer.recipient or "not-configured", delivery_status, datetime.now(UTC), error),
            )
            self._audit(connection, "notification_attempted", "kiwit-worker", {"status": delivery_status}, signal_id)
        return signal_id

    def get_signal(self, signal_id: UUID) -> dict[str, Any]:
        with self.database.connect(autocommit=True) as connection:
            row = connection.execute(
                "SELECT signal_id,symbol,exchange,regime,pattern,side,signal_at,expires_at,entry_price,stop_price,target_price,"
                "quantity,status,reviewed_by,reviewed_at,entry_fill_price,entry_filled_at,exit_fill_price,exit_filled_at,"
                "exit_reason,realized_pnl,rationale FROM intraday_signals WHERE signal_id=%s", (signal_id,),
            ).fetchone()
        if not row:
            raise KeyError(str(signal_id))
        keys = ("signal_id", "symbol", "exchange", "regime", "pattern", "side", "signal_at", "expires_at",
                "entry_price", "stop_price", "target_price", "quantity", "status", "reviewed_by", "reviewed_at",
                "entry_fill_price", "entry_filled_at", "exit_fill_price", "exit_filled_at", "exit_reason",
                "realized_pnl", "rationale")
        data = dict(zip(keys, row, strict=True))
        for key, value in tuple(data.items()):
            if isinstance(value, (datetime, UUID)):
                data[key] = value.isoformat() if isinstance(value, datetime) else str(value)
            elif isinstance(value, Decimal):
                data[key] = str(value)
        return data

    def list_signals(self, limit: int = 100) -> dict[str, Any]:
        with self.database.connect(autocommit=True) as connection:
            ids = connection.execute(
                "SELECT signal_id FROM intraday_signals ORDER BY signal_at DESC LIMIT %s", (limit,),
            ).fetchall()
            deliveries = connection.execute(
                "SELECT status,count(*) FROM notification_deliveries GROUP BY status"
            ).fetchall()
            audit_rows = connection.execute(
                "SELECT event_id,signal_id,event_type,actor,event_at,details FROM intraday_audit_events "
                "WHERE account_id=%s ORDER BY event_at DESC LIMIT 100", (self.settings.account_id,),
            ).fetchall()
            reconciliation = connection.execute(
                "SELECT trading_date,state,open_positions,pending_signals,entries,exits,realized_pnl,reconciled_at "
                "FROM daily_reconciliations WHERE account_id=%s ORDER BY trading_date DESC LIMIT 20",
                (self.settings.account_id,),
            ).fetchall()
        signals = [self.get_signal(row[0]) for row in ids]
        return {
            "execution": "paper-only", "signals": signals,
            "counts": {status: sum(1 for signal in signals if signal["status"] == status)
                       for status in ("pending", "entered", "exited", "rejected", "expired", "blocked")},
            "notifications": {row[0]: row[1] for row in deliveries},
            "audit": [
                {"event_id": str(row[0]), "signal_id": str(row[1]) if row[1] else None, "event_type": row[2],
                 "actor": row[3], "event_at": row[4].isoformat(), "details": row[5]}
                for row in audit_rows
            ],
            "reconciliations": [
                {"trading_date": str(row[0]), "state": row[1], "open_positions": row[2],
                 "pending_signals": row[3], "entries": row[4], "exits": row[5],
                 "realized_pnl": str(row[6]), "reconciled_at": row[7].isoformat()}
                for row in reconciliation
            ],
        }

    @serialized
    def review(self, signal_id: UUID, approved: bool, reviewer: str, reason: str, now: datetime | None = None,
               *, session_id: UUID | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        with self.database.transaction() as connection:
            signal = connection.execute(
                "SELECT symbol,expires_at,entry_price,quantity,status FROM intraday_signals WHERE signal_id=%s AND account_id=%s FOR UPDATE",
                (signal_id, self.settings.account_id),
            ).fetchone()
            if not signal:
                raise KeyError(str(signal_id))
            if signal[4] != "pending":
                raise ValueError(f"signal is already {signal[4]}")
            if now >= signal[1]:
                connection.execute("UPDATE intraday_signals SET status='expired',updated_at=%s WHERE signal_id=%s", (now, signal_id))
                self._audit(connection, "signal_expired", "kiwit-worker", {}, signal_id)
                raise ValueError("signal expired; wait for a fresh setup")
            if not approved:
                connection.execute(
                    "UPDATE intraday_signals SET status='rejected',reviewed_by=%s,reviewed_at=%s,review_reason=%s,updated_at=%s "
                    "WHERE signal_id=%s", (reviewer, now, reason, now, signal_id),
                )
                self._audit(connection, "signal_rejected", reviewer, {"reason": reason}, signal_id)
                return {"signal_id": str(signal_id), "status": "rejected", "execution": "paper-only"}
            # Share-lock also serializes this check with halt inserts/releases.
            # Existing exits deliberately remain possible while entries are halted.
            connection.execute("LOCK TABLE system_halts IN SHARE MODE")
            halted = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM system_halts WHERE active AND scope IN ('global',%s))",
                (self.settings.account_id,),
            ).fetchone()[0]
            if halted:
                raise ValueError("paper entries are halted; release the safety halt before approving")
            quote = connection.execute(
                "SELECT observed_at,ask_price FROM intraday_quotes WHERE symbol=%s ORDER BY observed_at DESC LIMIT 1",
                (signal[0],),
            ).fetchone()
            if not quote or not 0 <= (now - quote[0]).total_seconds() <= FRESHNESS_SECONDS:
                raise ValueError("latest quote is stale; approval blocked")
            fill = Decimal(quote[1]) * Decimal("1.0005")
            account = connection.execute(
                "SELECT cash_balance,status FROM paper_accounts WHERE account_id=%s FOR UPDATE", (self.settings.account_id,),
            ).fetchone()
            if not account:
                raise ValueError('paper account is unavailable')
            terms = self.session_entry_terms(connection, signal_id, session_id, fill, now, account[0])
            quantity, stop_fraction, target_fraction = terms or (signal[3], self.settings.stop_fraction, self.settings.target_fraction)
            stop = fill * (1 - stop_fraction)
            target = fill * (1 + target_fraction)
            turnover = fill * quantity
            fee = turnover * COST_RATE if session_id else Decimal(0)
            if account[1] != "active" or account[0] < turnover + fee:
                raise ValueError("paper account is unavailable or has insufficient cash")
            instrument_id = connection.execute(
                "SELECT instrument_id FROM instruments WHERE exchange='NSE' AND symbol=%s AND series='EQ' "
                "ORDER BY valid_from DESC LIMIT 1", (signal[0],),
            ).fetchone()
            if not instrument_id:
                instrument_id = uuid4()
                connection.execute(
                    "INSERT INTO instruments(instrument_id,exchange,symbol,series,lot_size,tick_size,valid_from) "
                    "VALUES(%s,'NSE',%s,'EQ',1,0.05,'2000-01-01')", (instrument_id, signal[0]),
                )
            else:
                instrument_id = instrument_id[0]
            connection.execute(
                "UPDATE paper_accounts SET cash_balance=cash_balance-%s,updated_at=%s WHERE account_id=%s",
                (turnover + fee, now, self.settings.account_id),
            )
            connection.execute(
                "INSERT INTO paper_positions(account_id,instrument_id,quantity,average_price,realized_pnl,updated_at) "
                "VALUES(%s,%s,%s,%s,0,%s) ON CONFLICT(account_id,instrument_id) DO UPDATE SET "
                "average_price=(paper_positions.average_price*paper_positions.quantity+EXCLUDED.average_price*EXCLUDED.quantity)/"
                "(paper_positions.quantity+EXCLUDED.quantity),quantity=paper_positions.quantity+EXCLUDED.quantity,updated_at=EXCLUDED.updated_at",
                (self.settings.account_id, instrument_id, quantity, fill, now),
            )
            connection.execute(
                "UPDATE intraday_signals SET status='entered',reviewed_by=%s,reviewed_at=%s,review_reason=%s,"
                "entry_fill_price=%s,entry_filled_at=%s,stop_price=%s,target_price=%s,quantity=%s,updated_at=%s WHERE signal_id=%s",
                (reviewer, now, reason, fill, now, stop, target, quantity, now, signal_id),
            )
            self._daily_trade(connection, now, turnover, fees=fee, realized=-fee)
            self._audit(connection, "paper_entry_filled", reviewer, {"price": str(fill), "quantity": quantity,
                        "entry_cost": str(fee), "session_id": str(session_id) if session_id else None}, signal_id)
        return self.get_signal(signal_id)

    def _daily_trade(self, connection: Any, now: datetime, turnover: Decimal,
                     *, fees: Decimal = Decimal(0), realized: Decimal = Decimal(0)) -> None:
        account = connection.execute(
            "SELECT cash_balance+COALESCE((SELECT sum(quantity*average_price) FROM paper_positions "
            "WHERE account_id=%s),0) FROM paper_accounts WHERE account_id=%s",
            (self.settings.account_id, self.settings.account_id),
        ).fetchone()
        connection.execute(
            "INSERT INTO paper_daily_ledger(account_id,trading_date,starting_equity,turnover,trade_count,updated_at,fees,realized_pnl) "
            "VALUES(%s,%s,%s,%s,1,%s,%s,%s) ON CONFLICT(account_id,trading_date) DO UPDATE SET "
            "turnover=paper_daily_ledger.turnover+EXCLUDED.turnover,trade_count=paper_daily_ledger.trade_count+1,"
            "fees=paper_daily_ledger.fees+EXCLUDED.fees,realized_pnl=paper_daily_ledger.realized_pnl+EXCLUDED.realized_pnl,"
            "updated_at=EXCLUDED.updated_at",
            (self.settings.account_id, now.astimezone(IST).date(), account[0]-realized, turnover, now, fees, realized),
        )

    @serialized
    def monitor_exits(self, now: datetime, *, force_reason: str | None = None, session_id: UUID | None = None) -> int:
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT signal_id,symbol,quantity,stop_price,target_price,entry_fill_price,session_id FROM intraday_signals "
                "WHERE account_id=%s AND status='entered' AND (%s::uuid IS NULL OR session_id=%s) FOR UPDATE",
                (self.settings.account_id, session_id, session_id),
            ).fetchall()
            exits = 0
            for signal_id, symbol, quantity, stop, target, entry, linked_session in rows:
                quote = connection.execute(
                    "SELECT observed_at,bid_price FROM intraday_quotes WHERE symbol=%s ORDER BY observed_at DESC LIMIT 1",
                    (symbol,),
                ).fetchone()
                if not quote or not 0 <= (now - quote[0]).total_seconds() <= FRESHNESS_SECONDS:
                    continue
                local = now.astimezone(IST)
                if local.weekday() >= 5 or not time(9,30) <= local.time() < time(15,15):
                    continue  # never fabricate a post-market fill
                reason = force_reason or ("stop_loss" if quote[1] <= stop else "take_profit" if quote[1] >= target else (
                    "end_of_day" if local.time() >= FLATTEN_TIME else None
                )
                )
                if reason is None:
                    continue
                fill = Decimal(quote[1]) / Decimal("1.0005")
                turnover = fill * quantity
                fee = turnover * COST_RATE if linked_session else Decimal(0)
                entry_fee = entry * quantity * COST_RATE if linked_session else Decimal(0)
                pnl = (fill - entry) * quantity - fee - entry_fee
                instrument_id = connection.execute(
                    "SELECT instrument_id FROM instruments WHERE exchange='NSE' AND symbol=%s AND series='EQ' "
                    "ORDER BY valid_from DESC LIMIT 1", (symbol,),
                ).fetchone()[0]
                connection.execute(
                    "UPDATE paper_positions SET quantity=quantity-%s,average_price=CASE WHEN quantity-%s=0 THEN 0 ELSE average_price END,"
                    "realized_pnl=realized_pnl+%s,updated_at=%s WHERE account_id=%s AND instrument_id=%s",
                    (quantity, quantity, pnl, now, self.settings.account_id, instrument_id),
                )
                connection.execute(
                    "UPDATE paper_accounts SET cash_balance=cash_balance+%s,realized_pnl=realized_pnl+%s,updated_at=%s "
                    "WHERE account_id=%s", (turnover - fee, pnl, now, self.settings.account_id),
                )
                connection.execute(
                    "UPDATE intraday_signals SET status='exited',exit_fill_price=%s,exit_filled_at=%s,exit_reason=%s,"
                    "realized_pnl=%s,updated_at=%s WHERE signal_id=%s", (fill, now, reason, pnl, now, signal_id),
                )
                self._daily_trade(connection, now, turnover, fees=fee, realized=pnl+entry_fee)
                self._audit(connection, "paper_exit_filled", "kiwit-worker", {
                    "price": str(fill), "quantity": quantity, "reason": reason, "realized_pnl": str(pnl),
                }, signal_id)
                exits += 1
            connection.execute(
                "UPDATE intraday_signals SET status='expired',updated_at=%s WHERE status='pending' AND expires_at<=%s",
                (now, now),
            )
        return exits

    def reconcile(self, now: datetime) -> None:
        trading_date = now.astimezone(IST).date()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT count(*) FILTER(WHERE status='entered'),count(*) FILTER(WHERE status='pending'),"
                "count(*) FILTER(WHERE status IN ('entered','exited')),count(*) FILTER(WHERE status='exited'),"
                "COALESCE(sum(realized_pnl) FILTER(WHERE status='exited'),0) FROM intraday_signals "
                "WHERE account_id=%s AND date(signal_at AT TIME ZONE 'Asia/Kolkata')=%s",
                (self.settings.account_id, trading_date),
            ).fetchone()
            state = "balanced" if row[0] == 0 and row[1] == 0 else "attention_required"
            connection.execute(
                "INSERT INTO daily_reconciliations(account_id,trading_date,state,open_positions,pending_signals,entries,exits,"
                "realized_pnl,reconciled_at,detail) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb) "
                "ON CONFLICT(account_id,trading_date) DO UPDATE SET state=EXCLUDED.state,open_positions=EXCLUDED.open_positions,"
                "pending_signals=EXCLUDED.pending_signals,entries=EXCLUDED.entries,exits=EXCLUDED.exits,"
                "realized_pnl=EXCLUDED.realized_pnl,reconciled_at=EXCLUDED.reconciled_at",
                (self.settings.account_id, trading_date, state, *row, now),
            )
            self._audit(connection, "daily_reconciliation", "kiwit-worker", {"state": state, "date": str(trading_date)})

    @serialized
    def run_once(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now(UTC)
        run_id = uuid4()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO intraday_worker_runs(run_id,started_at,state) VALUES(%s,%s,'running')", (run_id, now),
            )
        ingested = created = exits = 0
        state, detail = "completed", ""
        try:
            window = self._window_state(now)
            if not self.settings.enabled:
                state, detail = "disabled", "KIWIT_INTRADAY_ENABLED is false"
            elif window in {"entry_window", "reconciliation_window"}:
                errors = []
                for symbol in self.settings.symbols:
                    try:
                        self.ingest_quote(symbol, now)
                        ingested += 1
                    except Exception as error:  # noqa: BLE001 - keep exits available when one feed fails
                        explanation = ('Groww session approval required' if 'approval' in str(error).lower()
                                       else 'market data unavailable or stale')
                        errors.append(f'{symbol}: {type(error).__name__}: {explanation}')
                exits = self.monitor_exits(now)
                self.session_tick(now)
                if window == "entry_window" and now.astimezone(IST).time() < ENTRY_CUTOFF:
                    created = sum(self._create_signal(symbol, now) is not None for symbol in self.settings.symbols)
                    self.session_tick(now)
                if window == "reconciliation_window":
                    self.reconcile(now)
                if errors:
                    state, detail = 'data_unavailable', '; '.join(errors)
            else:
                state, detail = "outside_window", "Runs weekdays from 09:30 to 15:45 IST"
        except Exception as error:  # noqa: BLE001 - worker must persist every unexpected run failure
            state, detail = "failed", f"{type(error).__name__}: {error}"
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE intraday_worker_runs SET completed_at=%s,state=%s,quotes_ingested=%s,signals_created=%s,"
                "exits_created=%s,detail=%s WHERE run_id=%s", (datetime.now(UTC), state, ingested, created, exits, detail, run_id),
            )
        return {"run_id": str(run_id), "state": state, "quotes_ingested": ingested,
                "signals_created": created, "exits_created": exits, "detail": detail}
