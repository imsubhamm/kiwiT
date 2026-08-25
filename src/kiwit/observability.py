from __future__ import annotations

import json
import logging
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    """Emit one machine-readable event per line for journald/CloudWatch ingestion."""

    def format(self, record: logging.LogRecord) -> str:
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for name in ("request_id", "method", "path", "status_code", "duration_ms"):
            value = getattr(record, name, None)
            if value is not None:
                event[name] = value
        if record.exc_info:
            event["exception"] = self.formatException(record.exc_info)
        return json.dumps(event, separators=(",", ":"), default=str)


def configure_logging() -> None:
    root = logging.getLogger()
    if getattr(root, "_kiwit_configured", False):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel(logging.INFO)
    root._kiwit_configured = True  # type: ignore[attr-defined]


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.monotonic)
    requests: Counter[tuple[str, str, int]] = field(default_factory=Counter)
    durations: Counter[tuple[str, str]] = field(default_factory=Counter)
    in_flight: int = 0
    readiness_failures: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def begin(self) -> None:
        with self._lock:
            self.in_flight += 1

    def finish(self, method: str, route: str, status_code: int, duration: float) -> None:
        with self._lock:
            self.in_flight -= 1
            self.requests[(method, route, status_code)] += 1
            self.durations[(method, route)] += duration

    def readiness_failed(self) -> None:
        with self._lock:
            self.readiness_failures += 1

    def render(self) -> str:
        with self._lock:
            lines = [
                "# HELP kiwit_uptime_seconds Process uptime.",
                "# TYPE kiwit_uptime_seconds gauge",
                f"kiwit_uptime_seconds {time.monotonic() - self.started_at:.3f}",
                "# HELP kiwit_http_requests_in_flight Requests currently executing.",
                "# TYPE kiwit_http_requests_in_flight gauge",
                f"kiwit_http_requests_in_flight {self.in_flight}",
                "# HELP kiwit_readiness_failures_total Failed readiness checks.",
                "# TYPE kiwit_readiness_failures_total counter",
                f"kiwit_readiness_failures_total {self.readiness_failures}",
                "# HELP kiwit_http_requests_total HTTP requests by route and response status.",
                "# TYPE kiwit_http_requests_total counter",
            ]
            for (method, route, status), count in sorted(self.requests.items()):
                lines.append(f'kiwit_http_requests_total{{method="{method}",route="{route}",status="{status}"}} {count}')
            lines.extend((
                "# HELP kiwit_http_request_duration_seconds_total Cumulative request duration.",
                "# TYPE kiwit_http_request_duration_seconds_total counter",
            ))
            for (method, route), duration in sorted(self.durations.items()):
                lines.append(
                    f'kiwit_http_request_duration_seconds_total{{method="{method}",route="{route}"}} {duration:.6f}'
                )
        return "\n".join(lines) + "\n"
