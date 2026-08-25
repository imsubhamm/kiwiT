from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path


@dataclass(frozen=True)
class MembershipInterval:
    index_symbol: str
    symbol: str
    valid_from: date
    valid_to: date | None
    source_uri: str


@dataclass(frozen=True)
class InstrumentRecord:
    exchange: str
    symbol: str
    series: str
    isin: str | None
    valid_from: date
    valid_to: date | None = None


@dataclass(frozen=True)
class ObservedTradingCalendar:
    """Calendar derived from successfully matched exchange reports.

    This is not a substitute for an official forward holiday calendar.
    """

    sessions: frozenset[date]

    def is_session(self, day: date) -> bool:
        return day in self.sessions

    @property
    def latest_session(self) -> date | None:
        return max(self.sessions) if self.sessions else None


def load_corporate_action_dates(path: str | Path) -> set[date]:
    with Path(path).open(encoding="utf-8") as stream:
        return {date.fromisoformat(row["ex_date"]) for row in csv.DictReader(stream)}


def validate_membership(intervals: list[MembershipInterval]) -> None:
    grouped: dict[tuple[str, str], list[MembershipInterval]] = {}
    for interval in intervals:
        if interval.valid_to and interval.valid_to < interval.valid_from:
            raise ValueError("membership valid_to precedes valid_from")
        grouped.setdefault((interval.index_symbol, interval.symbol), []).append(interval)
    for key, values in grouped.items():
        ordered = sorted(values, key=lambda item: item.valid_from)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.valid_to is None or previous.valid_to >= current.valid_from:
                raise ValueError(f"overlapping membership intervals: {key}")


def validate_instruments(records: list[InstrumentRecord]) -> None:
    seen: set[tuple[str, str, str, date]] = set()
    for record in records:
        key = (record.exchange, record.symbol, record.series, record.valid_from)
        if key in seen:
            raise ValueError(f"duplicate instrument version: {key}")
        seen.add(key)
        if record.valid_to and record.valid_to < record.valid_from:
            raise ValueError("instrument valid_to precedes valid_from")
