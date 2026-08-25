from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from .promotion import PromotedStrategyCatalog

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class ScheduledRunResult:
    state: str
    strategy: str
    session_date: str
    signals: int = 0
    detail: str = ""


class UnattendedPaperScheduler:
    """Runs a promoted strategy once after the NSE close; never calls a live broker."""

    def __init__(
        self,
        catalog: PromotedStrategyCatalog,
        strategy_id: str,
        version: str,
        generate_signals: Callable[[datetime], list[Any]],
        submit_paper_signal: Callable[[Any], None],
    ) -> None:
        self.catalog = catalog
        self.strategy_id = strategy_id
        self.version = version
        self.generate_signals = generate_signals
        self.submit_paper_signal = submit_paper_signal
        self._completed_sessions: set[str] = set()

    def run(self, now: datetime) -> ScheduledRunResult:
        local = now.astimezone(IST)
        session = local.date().isoformat()
        strategy = f"{self.strategy_id}@{self.version}"
        if local.weekday() >= 5 or local.time() < time(15, 45):
            return ScheduledRunResult("outside_window", strategy, session, detail="Runs after 15:45 IST on weekdays")
        if session in self._completed_sessions:
            return ScheduledRunResult("already_completed", strategy, session, detail="Idempotent session guard")
        try:
            self.catalog.require_promoted(self.strategy_id, self.version)
        except PermissionError as error:
            return ScheduledRunResult("locked", strategy, session, detail=str(error))
        signals = self.generate_signals(local)
        for signal in signals:
            self.submit_paper_signal(signal)
        self._completed_sessions.add(session)
        return ScheduledRunResult("completed", strategy, session, signals=len(signals), detail="Paper-only submission")
