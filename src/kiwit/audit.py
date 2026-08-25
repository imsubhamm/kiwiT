from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class AuditEvent:
    event_id: str
    timestamp: str
    event_type: str
    payload: Mapping[str, Any]
    previous_hash: str
    event_hash: str


class HashChainAuditLog:
    """Append-only JSONL log with hash chaining for tamper evidence."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _last_hash(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return "GENESIS"
        with self.path.open("rb") as stream:
            last = None
            for line in stream:
                if line.strip():
                    last = line
        if last is None:
            return "GENESIS"
        return json.loads(last)["event_hash"]

    def append(self, event_type: str, payload: Mapping[str, Any]) -> AuditEvent:
        previous_hash = self._last_hash()
        body = {
            "event_id": str(uuid4()),
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "payload": payload,
            "previous_hash": previous_hash,
        }
        canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), default=str)
        event = AuditEvent(**body, event_hash=hashlib.sha256(canonical.encode()).hexdigest())
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(asdict(event), sort_keys=True, default=str) + "\n")
        return event

    def append_once(self, idempotency_key: str, event_type: str, payload: Mapping[str, Any]) -> AuditEvent:
        """Append once for retryable workflow nodes; an existing matching event is returned."""
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("payload", {}).get("idempotency_key") == idempotency_key:
                    return AuditEvent(
                        event_id=record["event_id"], timestamp=record["timestamp"], event_type=record["event_type"],
                        payload=record["payload"], previous_hash=record["previous_hash"], event_hash=record["event_hash"],
                    )
        return self.append(event_type, {**payload, "idempotency_key": idempotency_key})

    def verify(self) -> bool:
        previous_hash = "GENESIS"
        if not self.path.exists():
            return True
        for line in self.path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            event_hash = record.pop("event_hash")
            if record["previous_hash"] != previous_hash:
                return False
            canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str)
            if hashlib.sha256(canonical.encode()).hexdigest() != event_hash:
                return False
            previous_hash = event_hash
        return True
