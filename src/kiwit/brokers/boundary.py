"""Trusted paper orchestration boundary; no live broker capability."""

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from kiwit.domain import Decision, Instrument, RiskDecision, Side, TradeProposal
from kiwit.llm.critic import proposal_for_risk
from kiwit.marketdata.canonical import utc
from kiwit.ml.datasets import _hash, _json
from kiwit.ml.meta import candidate_for_risk_review
from kiwit.risk_policy import VERSION as RISK_VERSION
from kiwit.risk_policy import _encoded


@dataclass(frozen=True)
class ExecutionInstruction:
    payload: str
    signature: str


class ExecutionAdapter(Protocol):
    def submit(self, instruction: ExecutionInstruction, *, now): ...


class DisabledGrowwLiveAdapter:
    def __init__(self, *args, **kwargs):
        raise ValueError("LIVE is unavailable; future implementation and authorization required")


def execution_mode():
    if os.getenv("KIWIT_EXECUTION_MODE", "PAPER") != "PAPER":
        raise ValueError("Only PAPER execution is supported")
    return "PAPER"


class PaperExecutionBoundary:
    def __init__(self, simulator):
        execution_mode()
        self._simulator = simulator
        self._key = secrets.token_bytes(32)

    def authorize(self, *, meta, risk, proposal, critic, now):
        execution_mode()
        now = utc(now)
        candidate = candidate_for_risk_review(meta)
        if candidate is None:
            raise ValueError("Approved meta decision required")
        body = dict(risk)
        if (
            body.pop("fingerprint", None) != _hash(body)
            or risk["version"] != RISK_VERSION
            or risk["decision"] != "approve"
            or risk["mode"] != "PAPER"
            or type(risk["quantity"]) is not int
            or risk["quantity"] <= 0
            or risk["proposal_id"] != str(proposal.proposal_id)
            or utc(datetime.fromisoformat(risk["at"])) != now
            or utc(proposal.signal_timestamp) != now
        ):
            raise ValueError("Matching current risk approval required")
        expected = {
            "instrument": proposal.instrument.symbol,
            "side": proposal.side.value,
            "entry": str(proposal.entry_price),
            "stop": str(proposal.stop_price),
            "target": str(proposal.target_price),
            "signal_at": proposal.signal_timestamp.isoformat(),
        }
        if (
            risk["proposal"] != expected
            or candidate["strategy"] != proposal.strategy_id
            or UUID(candidate["candidate_id"][:32]) != proposal.proposal_id
            or proposal.side not in {Side.BUY, Side.SELL}
            or candidate["direction"] != ("LONG" if proposal.side == Side.BUY else "SHORT")
        ):
            raise ValueError("Decision/risk proposal mismatch")
        authoritative = {
            "candidate_id": candidate["candidate_id"],
            "direction": candidate["direction"],
            "entry": str(proposal.entry_price),
            "stop": str(proposal.stop_price),
            "target": str(proposal.target_price),
            "quantity": risk["quantity"],
            "confidence": candidate["confidence"],
            "thesis": candidate["thesis"],
        }
        if (
            utc(datetime.fromisoformat(critic["as_of"])) != now
            or proposal_for_risk(critic, authoritative, snapshot_fingerprint=meta["snapshot_fingerprint"]) is None
        ):
            raise ValueError("Current proposal-bound critic approval required")
        payload = {
            "version": "execution-instruction-v1",
            "mode": "PAPER",
            "at": now.isoformat(),
            "proposal_id": str(proposal.proposal_id),
            "strategy": proposal.strategy_id,
            "strategy_version": proposal.strategy_version,
            "instrument": _encoded(asdict(proposal.instrument)),
            "side": proposal.side.value,
            "entry": str(proposal.entry_price),
            "stop": str(proposal.stop_price),
            "target": str(proposal.target_price),
            "quantity": risk["quantity"],
            "risk_budget": risk["risk_budget"],
            "estimated_loss": risk["estimated_loss"],
            "meta_id": meta["meta_decision_id"],
            "risk_id": risk["fingerprint"],
        }
        encoded = _json(payload)
        return ExecutionInstruction(encoded, hmac.new(self._key, encoded.encode(), hashlib.sha256).hexdigest())

    def submit(self, instruction, *, now):
        execution_mode()
        if not isinstance(instruction, ExecutionInstruction):
            raise TypeError("Validated execution instruction required")
        signature = hmac.new(self._key, instruction.payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, instruction.signature):
            raise ValueError("Untrusted or modified execution instruction")
        data = json.loads(instruction.payload)
        if (
            data["version"] != "execution-instruction-v1"
            or data["mode"] != "PAPER"
            or utc(datetime.fromisoformat(data["at"])) != utc(now)
        ):
            raise ValueError("Wrong mode/version or expired instruction")
        values = [Decimal(data[k]) for k in ("entry", "stop", "target", "risk_budget", "estimated_loss")]
        if any(not value.is_finite() or value <= 0 for value in values):
            raise ValueError("Finite positive prices and budgets required")
        raw = data["instrument"]
        instrument = Instrument(**{**raw, "tick_size": Decimal(raw["tick_size"])})
        proposal = TradeProposal(
            data["strategy"],
            data["strategy_version"],
            instrument,
            Side(data["side"]),
            utc(now),
            *values[:3],
            proposal_id=UUID(data["proposal_id"]),
        )
        risk = RiskDecision(Decision.APPROVE, proposal.proposal_id, data["quantity"], *values[3:], ())
        return self._simulator.submit(proposal, risk, at=now)

    def execute_approved(self, *, meta, risk, proposal, critic, now):
        return self.submit(self.authorize(meta=meta, risk=risk, proposal=proposal, critic=critic, now=now), now=now)
