"""Shared point-in-time V2 paper/replay orchestration and deterministic reporting."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import UUID

from .domain import Decision, PortfolioSnapshot, Position, RiskDecision, Side, TradeProposal
from .llm.critic import proposal_for_risk
from .marketdata.canonical import IST, utc
from .marketdata.features import FeatureEngine
from .marketdata.history import HistoricalReplay, replay_sessions
from .ml.datasets import _hash
from .ml.meta import MetaDecisionEngine, candidate_for_risk_review
from .risk_policy import RiskState, _encoded
from .sizing import SizingInput, calculate_plan

VERSION = "unified-replay-v1"
KINDS = ("regime", "trend", "breakout", "mean_reversion")


class PaperDecisionCore:
    """One event method for replay or a paper service; all inputs supplied at the event time."""

    def __init__(
        self,
        *,
        history,
        calendar,
        instrument,
        execution_instrument,
        registry,
        journal,
        simulator,
        risk_policy,
        sizing_rules,
        audit,
        meta=None,
    ):
        if (
            instrument.symbol != execution_instrument.symbol
            or instrument.exchange != execution_instrument.exchange
            or simulator.calendar.version != calendar.version
            or risk_policy.calendar.version != calendar.version
        ):
            raise ValueError("Instrument/calendar bindings differ")
        if simulator.portfolio()["orders"]:
            raise ValueError("Start a fresh run; orchestration resume requires restoring decision state")
        self.history, self.calendar, self.instrument = history, calendar, instrument
        self.execution_instrument, self.registry, self.journal = execution_instrument, registry, journal
        self.simulator, self.risk_policy, self.sizing_rules, self.audit = simulator, risk_policy, sizing_rules, audit
        self.meta = meta or MetaDecisionEngine()
        self.features = FeatureEngine(calendar)
        self.previous, self.decisions, self.curve, self.order_context = [], [], [], {}
        self.models = None
        self.high_watermark = Decimal(simulator.portfolio()["equity"])

    def _portfolio(self, at):
        state = self.simulator.portfolio()
        equity = Decimal(state["equity"])
        self.high_watermark = max(self.high_watermark, equity)
        positions, daily, weekly, closed = [], Decimal(0), Decimal(0), []
        today = at.astimezone(IST).date()
        week_start = today - timedelta(days=today.weekday())
        trades = 0
        for order in state["orders"].values():
            if datetime.fromisoformat(order["submitted_at"]).astimezone(IST).date() == today:
                trades += 1  # Conservative: includes pending/rejected submissions.
            if order["status"] in {"OPEN", "EXIT_PENDING", "PENDING"}:
                entry = Decimal(order.get("entry", self.order_context[order["id"]]["entry"]))
                positions.append(
                    Position(
                        self.execution_instrument,
                        Side.BUY if order["long"] else Side.SELL,
                        order["quantity"],
                        entry,
                        Decimal(order["stop"]),
                        "v2",
                        at,
                    )
                )
            if order["status"] == "CLOSED":
                exit_at = datetime.fromisoformat(order["fills"][-1]["at"])
                net = Decimal(order["realized_pnl"])
                if exit_at.astimezone(IST).date() == today:
                    daily += net
                if week_start <= exit_at.astimezone(IST).date() <= today:
                    weekly += net
                closed.append((exit_at, order["id"], net))
        streak, last = 0, None
        for exit_at, _, pnl in sorted(closed, reverse=True):
            if pnl >= 0:
                break
            streak += 1
            last = last or exit_at
        return (
            PortfolioSnapshot(
                equity,
                self.high_watermark,
                daily,
                weekly,
                tuple(positions),
                sum((p.open_risk for p in positions), Decimal(0)),
            ),
            RiskState(at, today.isoformat(), trades, streak, last),
        )

    def process(self, *, at, quote=None, checks=None):
        at = utc(at)
        if self.curve and at <= datetime.fromisoformat(self.curve[-1]["at"]):
            raise ValueError("Decision events must strictly increase")
        if quote is not None:
            if quote.instrument != self.execution_instrument:
                raise ValueError("Wrong execution quote instrument")
            self.simulator.on_quote("quote:" + at.isoformat(), quote, received_at=at)
        session = self.calendar.session(at.astimezone(IST).date())
        if session is None:
            raise ValueError("Unknown replay session")
        frame = HistoricalReplay(self.history, self.calendar, clock=lambda: at).snapshot(
            self.instrument, session.bounds()[0], at
        )
        snapshot = self.features.compute(frame.result, self.instrument, at)
        scores = {kind: self.registry.score(kind, snapshot) for kind in KINDS}
        bindings = {kind: scores[kind].get("model_fingerprint") for kind in KINDS}
        if self.models is None:
            self.models = bindings
        if bindings != self.models:
            raise ValueError("Model selection changed inside a frozen run")
        for score in scores.values():
            cutoff = score.get("training_data_end")
            if cutoff is None or utc(datetime.fromisoformat(cutoff)) > at:
                score.update(status="BLOCKED", prediction=None, reason_codes=["MODEL_TRAINING_NOT_AVAILABLE"])
        checks = checks or {}
        meta = self.meta.evaluate(
            snapshot,
            scores,
            now=at,
            checks=checks,
            journal=self.journal,
            market=frame.result.candles,
            previous=self.previous,
        )
        candidate = candidate_for_risk_review(meta)
        result = {"at": at.isoformat(), "meta": meta, "status": "NO_TRADE"}
        if candidate and quote is not None:
            portfolio, state = self._portfolio(at)
            mark = quote.ask if candidate["direction"] == "LONG" else quote.bid
            rules = self.simulator.rules
            # Conservative cost allowance includes entry/exit slippage, fees and tick rounding.
            cost = mark * (rules.slippage_fraction + rules.fee_fraction) * 4 + self.execution_instrument.tick_size * 2
            sizing = calculate_plan(
                SizingInput(
                    self.instrument.key,
                    meta["snapshot_fingerprint"],
                    candidate["direction"],
                    mark,
                    portfolio.equity,
                    Decimal(self.simulator.portfolio()["cash"]),
                    self.execution_instrument.tick_size,
                    self.execution_instrument.lot_size,
                    Decimal(str(snapshot.values["atr_14"])),
                    cost,
                    mark * rules.fee_fraction,
                ),
                self.sizing_rules,
            )
            result["sizing"] = sizing
            if sizing["quantity"] > 0:
                authoritative = {
                    "candidate_id": candidate["candidate_id"],
                    "direction": candidate["direction"],
                    "entry": sizing["entry"],
                    "stop": sizing["stop"],
                    "target": sizing["target"],
                    "quantity": sizing["quantity"],
                    "confidence": candidate["confidence"],
                    "thesis": candidate["thesis"],
                }
                review = checks.get("critic_review", {})
                try:
                    review_is_current = utc(datetime.fromisoformat(review["as_of"])) == at
                except (KeyError, ValueError, TypeError):
                    review_is_current = False
                if (
                    not review_is_current
                    or proposal_for_risk(review, authoritative, snapshot_fingerprint=meta["snapshot_fingerprint"])
                    is None
                ):
                    result["status"] = "CRITIC_PROPOSAL_BINDING_REQUIRED"
                else:
                    proposal = TradeProposal(
                        candidate["strategy"],
                        "v2",
                        self.execution_instrument,
                        Side.BUY if candidate["direction"] == "LONG" else Side.SELL,
                        at,
                        Decimal(sizing["entry"]),
                        Decimal(sizing["stop"]),
                        Decimal(sizing["target"]),
                        proposal_id=UUID(candidate["candidate_id"][:32]),
                    )
                    policy = replace(
                        self.risk_policy,
                        limits=replace(
                            self.risk_policy.limits,
                            maximum_quantity=min(self.risk_policy.limits.maximum_quantity, sizing["quantity"]),
                        ),
                    )
                    risk = policy.evaluate(proposal, portfolio, cost, state=state, now=at, audit=self.audit)
                    result["risk"] = risk
                    if risk["decision"] == "approve" and risk["quantity"] == sizing["quantity"]:
                        decision = RiskDecision(
                            Decision.APPROVE,
                            proposal.proposal_id,
                            risk["quantity"],
                            Decimal(risk["risk_budget"]),
                            Decimal(risk["estimated_loss"]),
                            (),
                        )
                        self.order_context[str(proposal.proposal_id)] = {**candidate, "entry": sizing["entry"]}
                        result["order"] = self.simulator.submit(proposal, decision, at=at)
                        self.previous.append(candidate)
                        result["status"] = "SUBMITTED"
                    else:
                        result["status"] = "RISK_REJECTED_OR_RESIZING_REQUIRES_REVIEW"
        if at >= session.bounds()[1]:
            self.simulator.end_session(at=at)
        portfolio = self.simulator.portfolio()
        self.curve.append({"at": at.isoformat(), "equity": portfolio["equity"]})
        self.decisions.append(result)
        self.audit.append("unified_paper_decision", result)
        return result

    def report(self):
        portfolio = self.simulator.portfolio()
        closed = [o for o in portfolio["orders"].values() if o["status"] == "CLOSED"]

        def metrics(orders):
            pnl = [Decimal(o["realized_pnl"]) for o in orders]
            return {
                "trades": len(pnl),
                "wins": sum(p > 0 for p in pnl),
                "losses": sum(p < 0 for p in pnl),
                "net_pnl": str(sum(pnl, Decimal(0))),
                "expectancy": str(sum(pnl) / len(pnl)) if pnl else None,
            }

        peak, drawdown = Decimal(portfolio["metadata"]["initial_capital"]), Decimal(0)
        for point in self.curve:
            equity = Decimal(point["equity"])
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak - equity) / peak)
        report = {
            "version": VERSION,
            "model_bindings": self.models,
            "configuration": _encoded(
                {
                    "risk": asdict(self.risk_policy.risk),
                    "limits": asdict(self.risk_policy.limits),
                    "sizing": asdict(self.sizing_rules),
                    "meta": asdict(self.meta.config),
                    "calendar": self.calendar.version,
                }
            ),
            "portfolio": portfolio,
            "equity_curve": self.curve,
            "decisions": self.decisions,
            "metrics": {**metrics(closed), "maximum_drawdown": str(drawdown)},
            "by_strategy": {},
            "by_regime": {},
        }
        for field in ("strategy", "regime"):
            keys = sorted({self.order_context[o["id"]][field] for o in closed})
            report["by_" + field] = {
                key: metrics([o for o in closed if self.order_context[o["id"]][field] == key]) for key in keys
            }
        report["fingerprint"] = _hash(report)
        return report


def run_backtest(core, *, start, end, tape):
    """Tape maps exact decision timestamps to already-available quotes/checks; no synthetic quotes."""
    for frame in replay_sessions(core.history, core.calendar, core.instrument, start, end):
        event = tape.get(frame.as_of.isoformat(), {})
        core.process(at=frame.as_of, quote=event.get("quote"), checks=event.get("checks"))
    return core.report()
