"""V2 decision evidence, append-only paper lifecycle, and delayed outcome labels."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from .marketdata.canonical import IST, Candle, Instrument, TradingCalendar, ordered_candles, utc
from .marketdata.features import FeatureSnapshot
from .marketdata.history import HistoricalStore, _micros

HORIZONS = (5, 15, 30, 60)
_SECRET_KEYS = re.compile(r"authorization|password|passwd|secret|token|api.?key|cookie|credential", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
_KEY_VALUE = re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|password|secret)\s*[:=]\s*[^\s,;]+")


def _json(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()


class Redactor:
    """Remove sensitive fields and known secret literals before any journal write."""

    def __init__(self, secrets: Iterable[str] = ()):
        self._secrets = tuple(sorted((s for s in secrets if s), key=len, reverse=True))

    def clean(self, value):
        if isinstance(value, Mapping):
            return {str(k): "[REDACTED]" if _SECRET_KEYS.search(str(k)) else self.clean(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.clean(v) for v in value]
        if isinstance(value, str):
            for secret in self._secrets:
                value = value.replace(secret, "[REDACTED]")
            return _KEY_VALUE.sub(r"\1=[REDACTED]", _BEARER.sub("Bearer [REDACTED]", value))
        return value


class DecisionJournal:
    def __init__(self, path: str | Path, history: HistoricalStore, *, secrets: Iterable[str] = ()):
        self.path, self.history = Path(path), history
        self._redactor = Redactor(secrets)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS decisions (
                    decision_id TEXT PRIMARY KEY, at_us INTEGER NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS decisions_at ON decisions(at_us, decision_id);
                CREATE TABLE IF NOT EXISTS decision_events (
                    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
                    event_id TEXT NOT NULL, at_us INTEGER NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(decision_id, event_id)
                );
                CREATE TABLE IF NOT EXISTS decision_labels (
                    decision_id TEXT NOT NULL REFERENCES decisions(decision_id),
                    horizon INTEGER NOT NULL, available_us INTEGER NOT NULL,
                    payload TEXT NOT NULL, digest TEXT NOT NULL,
                    PRIMARY KEY(decision_id, horizon)
                );
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _verified(row):
        if _digest(row[0]) != row[1]:
            raise ValueError("Journal evidence checksum mismatch")
        return json.loads(row[0])

    def record(
        self,
        decision_id: str,
        features: FeatureSnapshot,
        market: Iterable[Candle],
        *,
        action: str,
        reason_codes: Iterable[str],
        models: list[dict],
        candidate: dict | None = None,
        llm: dict | None = None,
        risk_checks: dict | None = None,
    ) -> str:
        if not decision_id or action not in {"TRADE", "NO_TRADE", "REJECT"}:
            raise ValueError("Decision identity/action is required")
        if isinstance(reason_codes, (str, bytes)):
            raise TypeError("Reason codes must be a collection, not a string")
        reasons = tuple(reason_codes)
        if not reasons or any(not isinstance(r, str) or not r for r in reasons):
            raise ValueError("Decision reason codes are required")
        if action == "TRADE" and (not features.ready or candidate is None):
            raise ValueError("Trade candidate requires ready features and a candidate")
        for model in models:
            if not model.get("version") or not isinstance(model.get("scores"), dict):
                raise ValueError("Models require version and structured scores")
        if llm is not None and not {"prompt", "prompt_version", "model_version", "output"} <= llm.keys():
            raise ValueError("LLM evidence requires prompt, versions and output")
        bars = ordered_candles(market)
        if any(c.instrument != features.instrument or c.closed_at > features.as_of for c in bars):
            raise ValueError("Invalid instrument or future decision input")
        input_digest = hashlib.sha256(json.dumps([c.to_json_dict() for c in bars], sort_keys=True).encode()).hexdigest()
        if input_digest != features.input_digest:
            raise ValueError("Market snapshot does not match feature input digest")
        references = self.history.provenance(bars, as_of=features.as_of)
        payload = self._redactor.clean(
            {
                "schema_version": "decision-v1",
                "decision_id": decision_id,
                "at": features.as_of.isoformat(),
                "instrument": {
                    "symbol": features.instrument.symbol,
                    "exchange": features.instrument.exchange,
                    "segment": features.instrument.segment,
                    "series": features.instrument.series,
                    "isin": features.instrument.isin,
                },
                "raw_snapshot_references": references,
                "market": [c.to_json_dict() for c in bars],
                "features": features.to_json_dict(),
                "models": models,
                "candidate": candidate,
                "action": action,
                "reason_codes": reasons,
                "llm": llm,
                "risk_checks": risk_checks,
                "reference_price": str(bars[-1].close) if bars else None,
                "reference_at": bars[-1].closed_at.isoformat() if bars else None,
            }
        )
        encoded = _json(payload)
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload,digest FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
            if old:
                self._verified(old)
                if old[0] != encoded:
                    raise ValueError("Decision identity already has different evidence")
            else:
                db.execute(
                    "INSERT INTO decisions VALUES (?, ?, ?, ?)",
                    (decision_id, _micros(features.as_of), encoded, _digest(encoded)),
                )
        return decision_id

    def append_event(self, decision_id: str, event_id: str, *, at: datetime, kind: str, details: dict):
        """Append risk/execution evidence, never update the original decision."""
        if kind not in {"risk", "execution"} or not event_id:
            raise ValueError("Invalid lifecycle event")
        at = utc(at)
        if kind == "execution":
            if details.get("mode") != "PAPER":
                raise ValueError("Only paper execution evidence is supported")
            if not {"status", "fills", "position_id"} <= details.keys():
                raise ValueError("Execution evidence needs status, fills and position identity")
            if not isinstance(details["fills"], list):
                raise ValueError("Execution fills must be a list")
            for fill in details["fills"]:
                if not {"instrument", "at", "price", "quantity", "side", "fees"} <= fill.keys():
                    raise ValueError("Fill evidence requires instrument, time, price, quantity, side and fees")
                fill_at = utc(datetime.fromisoformat(fill["at"]))
                if fill_at > at or fill["side"] not in {"BUY", "SELL"}:
                    raise ValueError("Invalid fill time or side")
                for key in ("price", "quantity", "fees"):
                    value = Decimal(str(fill[key]))
                    if not value.is_finite() or value < 0 or (key != "fees" and value == 0):
                        raise ValueError("Invalid fill amount")
        encoded = _json(self._redactor.clean({"kind": kind, "at": at.isoformat(), "details": details}))
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            decision = db.execute("SELECT at_us FROM decisions WHERE decision_id=?", (decision_id,)).fetchone()
            if decision is None or _micros(at) < decision[0]:
                raise ValueError("Event precedes or lacks a decision")
            old = db.execute(
                "SELECT payload,digest FROM decision_events WHERE decision_id=? AND event_id=?", (decision_id, event_id)
            ).fetchone()
            if old:
                self._verified(old)
                if old[0] != encoded:
                    raise ValueError("Event identity already has different evidence")
            else:
                db.execute(
                    "INSERT INTO decision_events VALUES (?, ?, ?, ?, ?)",
                    (decision_id, event_id, _micros(at), encoded, _digest(encoded)),
                )

    def read(self, decision_id: str, *, as_of: datetime) -> dict:
        cutoff = _micros(as_of)
        with self._connect() as db:
            row = db.execute(
                "SELECT payload,digest FROM decisions WHERE decision_id=? AND at_us<=?", (decision_id, cutoff)
            ).fetchone()
            if row is None:
                raise KeyError("Decision unavailable at this clock")
            decision = self._verified(row)
            events = db.execute(
                "SELECT payload,digest FROM decision_events WHERE decision_id=? AND at_us<=? ORDER BY at_us,event_id",
                (decision_id, cutoff),
            ).fetchall()
            labels = db.execute(
                "SELECT payload,digest FROM decision_labels WHERE decision_id=? AND available_us<=? ORDER BY horizon",
                (decision_id, cutoff),
            ).fetchall()
        return {
            "decision": decision,
            "events": [self._verified(r) for r in events],
            "outcomes": [self._verified(r) for r in labels],
        }

    def export(self, *, as_of: datetime) -> list[dict]:
        with self._connect() as db:
            ids = db.execute(
                "SELECT decision_id FROM decisions WHERE at_us<=? ORDER BY at_us,decision_id", (_micros(as_of),)
            ).fetchall()
        return [self.read(row[0], as_of=as_of) for row in ids]

    def label_outcomes(self, decision_id: str, *, as_of: datetime, calendar: TradingCalendar) -> dict[int, str]:
        """Observed forward close returns, not realized PnL or simulated fills."""
        now = utc(as_of)
        record = self.read(decision_id, as_of=now)
        decision = record["decision"]
        at = utc(datetime.fromisoformat(decision["at"]))
        instrument = Instrument(**decision["instrument"])
        session = calendar.session(at.astimezone(IST).date())
        results = {}
        existing = {r["horizon_minutes"] for r in record["outcomes"]}
        for horizon in HORIZONS:
            if horizon in existing:
                results[horizon] = "LABELED"
                continue
            target = at + timedelta(minutes=horizon)
            if target > now:
                results[horizon] = "PENDING"
                continue
            if session is None or not session.bounds()[0] <= at < target <= session.bounds()[1]:
                results[horizon] = "OUTSIDE_SESSION"
                continue
            if decision["reference_price"] is None or decision["reference_at"] != at.isoformat():
                results[horizon] = "MISSING_REFERENCE"
                continue
            candles = self.history.candles(instrument, at, target, as_of=now)
            expected = tuple(at + timedelta(minutes=i) for i in range(horizon))
            if tuple(c.opened_at for c in candles) != expected:
                results[horizon] = "MISSING_OUTCOME_DATA"
                continue
            close = candles[-1].close
            forward = close / Decimal(decision["reference_price"]) - 1
            direction = (decision["candidate"] or {}).get("side")
            signed = forward if direction in {"LONG", "BUY"} else -forward if direction in {"SHORT", "SELL"} else None
            label = {
                "label_version": "forward-close-v1",
                "calendar_version": calendar.version,
                "horizon_minutes": horizon,
                "target_at": target.isoformat(),
                "available_at": now.isoformat(),
                "reference_price": decision["reference_price"],
                "close": str(close),
                "forward_return": str(forward),
                "directional_return": None if signed is None else str(signed),
                "movement": "UP" if forward > 0 else "DOWN" if forward < 0 else "FLAT",
                "raw_snapshot_references": self.history.provenance(candles, as_of=now),
            }
            encoded = _json(label)
            with self._connect() as db:
                db.execute(
                    "INSERT OR IGNORE INTO decision_labels VALUES (?, ?, ?, ?, ?)",
                    (decision_id, horizon, _micros(now), encoded, _digest(encoded)),
                )
            results[horizon] = "LABELED"
        return results


class FeatureDecisionAudit:
    """Adapter for FeatureEngine.decide; rejected and warm-up decisions are retained."""

    def __init__(
        self,
        journal: DecisionJournal,
        decision_id: str,
        features: FeatureSnapshot,
        market: Iterable[Candle],
        *,
        candidate: dict | None = None,
        llm: dict | None = None,
        risk_checks: dict | None = None,
    ):
        self.journal, self.decision_id, self.features = journal, decision_id, features
        self.market, self.candidate, self.llm, self.risk_checks = tuple(market), candidate, llm, risk_checks

    def append(self, event_type: str, payload: Mapping):
        if event_type != "feature_model_decision" or payload["features"] != self.features.to_json_dict():
            raise ValueError("Feature decision audit context mismatch")
        decision = payload["decision"]
        reason_codes = decision.get("reason_codes") or [decision.get("reason") or "MODEL_DECISION"]
        return self.journal.record(
            self.decision_id,
            self.features,
            self.market,
            action=decision["action"],
            reason_codes=reason_codes,
            models=[
                {
                    "version": payload["model_id"],
                    "scores": decision.get("scores", {"score": decision["score"]} if "score" in decision else {}),
                    "output": decision,
                }
            ],
            candidate=self.candidate,
            llm=self.llm,
            risk_checks=self.risk_checks,
        )
