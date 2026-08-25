from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .database import PostgresDatabase
from .domain import Instrument, Quote, RiskDecision, Side, TradeProposal
from .execution import PaperFill
from .operations import build_operational_report
from .persistence import canonical_json


@dataclass(frozen=True)
class PaperCostModel:
    slippage_bps: Decimal = Decimal(5)
    brokerage_bps: Decimal = Decimal(0)
    taxes_bps: Decimal = Decimal(3)

    def __post_init__(self) -> None:
        if min(self.slippage_bps, self.brokerage_bps, self.taxes_bps) < 0:
            raise ValueError("paper costs cannot be negative")

    def fill_price(self, quote: Quote, side: Side) -> Decimal:
        multiplier = Decimal(1) + self.slippage_bps / Decimal(10000)
        return quote.ask * multiplier if side == Side.BUY else quote.bid / multiplier

    def charges(self, turnover: Decimal) -> tuple[Decimal, Decimal]:
        return turnover * self.brokerage_bps / Decimal(10000), turnover * self.taxes_bps / Decimal(10000)


class PostgresPaperLedger:
    """Atomic, idempotent paper ledger. It cannot route orders to a live broker."""

    def __init__(self, database: PostgresDatabase, cost_model: PaperCostModel | None = None) -> None:
        self.database = database
        self.cost_model = cost_model or PaperCostModel()

    def create_account(self, account_id: str, initial_cash: Decimal) -> None:
        if not account_id or initial_cash <= 0:
            raise ValueError("invalid paper account")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO paper_accounts(account_id,initial_cash,cash_balance,status) VALUES(%s,%s,%s,'active') "
                "ON CONFLICT(account_id) DO NOTHING", (account_id, initial_cash, initial_cash),
            )

    def halt(self, scope: str, reason_code: str, reason: str) -> None:
        if not scope or not reason_code or not reason:
            raise ValueError("halt scope, code, and reason are required")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO system_halts(halt_id,scope,reason_code,reason,activated_at) VALUES(%s,%s,%s,%s,%s) "
                "ON CONFLICT DO NOTHING", (uuid4(), scope, reason_code, reason, datetime.now(UTC)),
            )

    def release_halt(self, scope: str, released_by: str) -> None:
        if not released_by:
            raise ValueError("released_by is required")
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE system_halts SET active=false,released_at=%s,released_by=%s WHERE scope=%s AND active",
                (datetime.now(UTC), released_by, scope),
            )

    def register_strategy(self, strategy_id: str, version: str, specification: dict[str, Any]) -> None:
        payload = canonical_json(specification)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO strategy_versions(strategy_id,version,status,specification,specification_sha256) "
                "VALUES(%s,%s,'paper',%s::jsonb,%s) ON CONFLICT(strategy_id,version) DO NOTHING",
                (strategy_id, version, payload, digest),
            )

    def register_instrument(self, instrument: Instrument, *, valid_from: str = "2000-01-01") -> UUID:
        instrument_id = uuid5(
            NAMESPACE_URL, f"{instrument.exchange}:{instrument.symbol}:{instrument.series}:{valid_from}"
        )
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO instruments(instrument_id,exchange,symbol,series,lot_size,tick_size,valid_from) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(exchange,symbol,series,valid_from) DO NOTHING",
                (instrument_id, instrument.exchange, instrument.symbol, instrument.series,
                 instrument.lot_size, instrument.tick_size, valid_from),
            )
            row = connection.execute(
                "SELECT instrument_id FROM instruments WHERE exchange=%s AND symbol=%s AND series=%s AND valid_from=%s",
                (instrument.exchange, instrument.symbol, instrument.series, valid_from),
            ).fetchone()
        return row[0]

    def stage_proposal(self, proposal: TradeProposal, instrument_id: UUID) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO trade_proposals(proposal_id,idempotency_key,strategy_id,strategy_version,instrument_id,side,"
                "signal_at,entry_price,stop_price,target_price,status,rationale) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'received',%s::jsonb) ON CONFLICT(proposal_id) DO NOTHING",
                (proposal.proposal_id, f"proposal:{proposal.proposal_id}", proposal.strategy_id, proposal.strategy_version,
                 instrument_id, proposal.side.value, proposal.signal_timestamp, proposal.entry_price, proposal.stop_price,
                 proposal.target_price, canonical_json(proposal.rationale)),
            )

    def record_risk(self, decision: RiskDecision, *, rules_version: str = "risk-v1") -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO risk_decisions(risk_decision_id,proposal_id,decision,quantity,risk_budget,estimated_loss,"
                "reason_codes,rules_version,decided_at) VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s) "
                "ON CONFLICT(proposal_id,rules_version) DO NOTHING",
                (uuid4(), decision.proposal_id, decision.decision.value, decision.quantity, decision.risk_budget,
                 decision.estimated_loss, json.dumps(list(decision.reason_codes)), rules_version, datetime.now(UTC)),
            )
            status = "awaiting_human" if decision.quantity > 0 and decision.decision.value == "approve" else "risk_rejected"
            connection.execute("UPDATE trade_proposals SET status=%s WHERE proposal_id=%s AND status='received'", (status, decision.proposal_id))

    def record_review(self, proposal_id: UUID, approved: bool, reviewer: str, reason: str = "") -> None:
        if not reviewer:
            raise ValueError("reviewer is required")
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO human_reviews(review_id,proposal_id,approved,reviewer,reason,reviewed_at) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(proposal_id) DO NOTHING",
                (uuid4(), proposal_id, approved, reviewer, reason, datetime.now(UTC)),
            )
            status = "approved" if approved else "expired"
            connection.execute(
                "UPDATE trade_proposals SET status=%s WHERE proposal_id=%s AND status='awaiting_human'", (status, proposal_id)
            )

    def execute(self, account_id: str, proposal: TradeProposal, risk: RiskDecision, quote: Quote) -> PaperFill:
        if risk.proposal_id != proposal.proposal_id or risk.quantity <= 0:
            raise ValueError("approved positive risk decision required")
        if quote.instrument != proposal.instrument:
            raise ValueError("quote instrument mismatch")
        price = self.cost_model.fill_price(quote, proposal.side)
        turnover = price * risk.quantity
        fees, taxes = self.cost_model.charges(turnover)
        total_charges = fees + taxes
        now = datetime.now(UTC)
        with self.database.transaction() as connection:
            connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (f"paper:{account_id}",))
            existing = connection.execute(
                "SELECT f.fill_id,f.filled_at,o.side,f.quantity,f.price FROM fills f JOIN broker_orders o USING(order_id) "
                "WHERE o.proposal_id=%s AND o.environment='paper'", (proposal.proposal_id,),
            ).fetchone()
            if existing:
                return PaperFill(existing[0], proposal.proposal_id, existing[1], Side(existing[2]), int(existing[3]), existing[4])
            blocked = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM system_halts WHERE active AND scope IN ('global',%s))", (account_id,)
            ).fetchone()[0]
            account = connection.execute(
                "SELECT cash_balance,status FROM paper_accounts WHERE account_id=%s FOR UPDATE", (account_id,)
            ).fetchone()
            approved = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM trade_proposals p JOIN human_reviews h USING(proposal_id) "
                "JOIN risk_decisions r USING(proposal_id) WHERE p.proposal_id=%s AND p.status='approved' "
                "AND h.approved AND r.decision='approve' AND r.quantity=%s)", (proposal.proposal_id, risk.quantity),
            ).fetchone()[0]
            if blocked or not account or account[1] != "active" or not approved:
                raise ValueError("paper execution is halted or not approved")
            instrument_id = connection.execute(
                "SELECT instrument_id FROM trade_proposals WHERE proposal_id=%s", (proposal.proposal_id,)
            ).fetchone()[0]
            position = connection.execute(
                "SELECT quantity,average_price,realized_pnl FROM paper_positions WHERE account_id=%s AND instrument_id=%s FOR UPDATE",
                (account_id, instrument_id),
            ).fetchone()
            old_qty, old_average, realized = position or (Decimal(0), Decimal(0), Decimal(0))
            quantity = Decimal(risk.quantity)
            if proposal.side == Side.BUY:
                cash_change = -(turnover + total_charges)
                if account[0] + cash_change < 0:
                    raise ValueError("insufficient paper cash")
                new_qty = old_qty + quantity
                new_average = (old_qty * old_average + turnover) / new_qty
                realized_delta = Decimal(0)
            else:
                if old_qty < quantity:
                    raise ValueError("paper account cannot sell more than its long position")
                cash_change = turnover - total_charges
                new_qty = old_qty - quantity
                new_average = old_average if new_qty else Decimal(0)
                realized_delta = (price - old_average) * quantity - total_charges
            order_id, fill_id = uuid4(), uuid4()
            connection.execute(
                "INSERT INTO broker_orders(order_id,proposal_id,client_order_id,broker,broker_order_id,environment,order_type,"
                "side,quantity,status,submitted_at,updated_at,raw_response,account_id) "
                "VALUES(%s,%s,%s,'kiwit-paper',%s,'paper','market',%s,%s,'filled',%s,%s,'{}'::jsonb,%s)",
                (order_id, proposal.proposal_id, f"paper:{proposal.proposal_id}", str(order_id), proposal.side.value,
                 risk.quantity, now, now, account_id),
            )
            connection.execute(
                "INSERT INTO fills(fill_id,order_id,broker_fill_id,filled_at,quantity,price,fees,taxes,raw_response) "
                "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb)",
                (fill_id, order_id, str(fill_id), now, risk.quantity, price, fees, taxes),
            )
            connection.execute(
                "INSERT INTO paper_positions(account_id,instrument_id,quantity,average_price,realized_pnl,updated_at) "
                "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT(account_id,instrument_id) DO UPDATE SET quantity=EXCLUDED.quantity,"
                "average_price=EXCLUDED.average_price,realized_pnl=paper_positions.realized_pnl+%s,updated_at=EXCLUDED.updated_at",
                (account_id, instrument_id, new_qty, new_average, realized + realized_delta, now, realized_delta),
            )
            connection.execute(
                "UPDATE paper_accounts SET cash_balance=cash_balance+%s,realized_pnl=realized_pnl+%s,updated_at=%s WHERE account_id=%s",
                (cash_change, realized_delta, now, account_id),
            )
            connection.execute(
                "UPDATE trade_proposals SET status='submitted' WHERE proposal_id=%s", (proposal.proposal_id,)
            )
            connection.execute(
                "INSERT INTO paper_daily_ledger(account_id,trading_date,starting_equity,fees,turnover,trade_count,updated_at) "
                "VALUES(%s,%s,%s,%s,%s,1,%s) ON CONFLICT(account_id,trading_date) DO UPDATE SET "
                "fees=paper_daily_ledger.fees+EXCLUDED.fees,turnover=paper_daily_ledger.turnover+EXCLUDED.turnover,"
                "trade_count=paper_daily_ledger.trade_count+1,updated_at=EXCLUDED.updated_at",
                (account_id, now.date(), account[0], total_charges, turnover, now),
            )
        return PaperFill(fill_id, proposal.proposal_id, now, proposal.side, risk.quantity, price)

    def fill_for(self, proposal_id: UUID) -> PaperFill | None:
        with self.database.connect(autocommit=True) as connection:
            row = connection.execute(
                "SELECT f.fill_id,f.filled_at,o.side,f.quantity,f.price FROM fills f JOIN broker_orders o USING(order_id) "
                "WHERE o.proposal_id=%s AND o.environment='paper'", (proposal_id,),
            ).fetchone()
        if not row:
            return None
        return PaperFill(row[0], proposal_id, row[1], Side(row[2]), int(row[3]), row[4])

    def account_status(self, account_id: str) -> dict[str, Any]:
        with self.database.connect(autocommit=True) as connection:
            account = connection.execute(
                "SELECT account_id,currency,initial_cash,cash_balance,realized_pnl,status FROM paper_accounts WHERE account_id=%s",
                (account_id,),
            ).fetchone()
            if not account:
                raise KeyError(account_id)
            positions = connection.execute(
                "SELECT i.symbol,p.quantity,p.average_price,p.realized_pnl FROM paper_positions p "
                "JOIN instruments i USING(instrument_id) WHERE p.account_id=%s AND p.quantity>0 ORDER BY i.symbol", (account_id,),
            ).fetchall()
            halted = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM system_halts WHERE active AND scope IN ('global',%s))", (account_id,)
            ).fetchone()[0]
        return {
            "account_id": account[0], "currency": account[1], "initial_cash": str(account[2]),
            "cash_balance": str(account[3]), "realized_pnl": str(account[4]), "status": account[5],
            "execution_halted": halted,
            "positions": [
                {"symbol": row[0], "quantity": str(row[1]), "average_price": str(row[2]), "realized_pnl": str(row[3])}
                for row in positions
            ],
        }

    def operational_report(self, account_id: str) -> dict[str, Any]:
        with self.database.connect(autocommit=True) as connection:
            account = connection.execute(
                "SELECT initial_cash,cash_balance,realized_pnl FROM paper_accounts WHERE account_id=%s", (account_id,),
            ).fetchone()
            if not account:
                raise KeyError(account_id)
            daily = connection.execute(
                "SELECT trading_date,starting_equity+realized_pnl,fees,turnover,trade_count "
                "FROM paper_daily_ledger WHERE account_id=%s ORDER BY trading_date", (account_id,),
            ).fetchall()
            incidents = connection.execute(
                "SELECT scope,reason_code,reason,active,activated_at,released_at FROM system_halts "
                "WHERE scope IN ('global',%s) ORDER BY activated_at DESC LIMIT 50", (account_id,),
            ).fetchall()
            positions = connection.execute(
                "SELECT count(*) FROM paper_positions WHERE account_id=%s AND quantity>0", (account_id,),
            ).fetchone()[0]
        return build_operational_report(
            account_id=account_id, initial_cash=account[0], cash_balance=account[1], realized_pnl=account[2],
            daily_rows=daily, incident_rows=incidents, positions=positions,
        )


class AccountPaperBroker:
    """Binds one paper account to the broker interface consumed by LangGraph."""

    def __init__(self, ledger: PostgresPaperLedger, account_id: str) -> None:
        if not account_id:
            raise ValueError("paper account ID is required")
        self.ledger = ledger
        self.account_id = account_id

    def fill_for(self, proposal_id: UUID) -> PaperFill | None:
        return self.ledger.fill_for(proposal_id)

    def execute(self, proposal: TradeProposal, risk: RiskDecision, quote: Quote) -> PaperFill:
        return self.ledger.execute(self.account_id, proposal, risk, quote)
