"""Indexed immutable candle history and point-in-time replay for market-v1."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .canonical import IST, Candle, Instrument, TradingCalendar, ordered_candles, utc
from .live import IngestResult

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _micros(value: datetime) -> int:
    delta = utc(value) - _EPOCH
    return (delta.days * 86400 + delta.seconds) * 1_000_000 + delta.microseconds


def _interval_us(interval: timedelta) -> int:
    value = (interval.days * 86400 + interval.seconds) * 1_000_000 + interval.microseconds
    if value <= 0:
        raise ValueError("Timeframe must be positive")
    return value


def _identity(instrument: Instrument) -> str:
    # JSON tuple avoids delimiter collisions in source instrument identifiers.
    return json.dumps([instrument.exchange, instrument.segment, instrument.series, instrument.symbol])


class HistoricalStore:
    """One local SQLite database, indexed by instrument, session date and timeframe.

    Availability is mandatory and immutable. A corrected candle is rejected rather
    than silently replacing the historical value with hindsight. Raw batches are
    immutable and linked to normalized observations in the same transaction.
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS historical_raw (
                    digest TEXT PRIMARY KEY,
                    source TEXT NOT NULL,
                    received_us INTEGER NOT NULL,
                    body BLOB NOT NULL
                );
                CREATE TABLE IF NOT EXISTS historical_candles (
                    instrument TEXT NOT NULL,
                    session_date TEXT NOT NULL,
                    interval_us INTEGER NOT NULL,
                    opened_us INTEGER NOT NULL,
                    closed_us INTEGER NOT NULL,
                    available_us INTEGER NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (instrument, interval_us, opened_us)
                );
                CREATE INDEX IF NOT EXISTS history_session_timeframe
                    ON historical_candles(instrument, session_date, interval_us, opened_us);
                CREATE INDEX IF NOT EXISTS history_as_of
                    ON historical_candles(instrument, interval_us, opened_us, closed_us, available_us);
                CREATE TABLE IF NOT EXISTS historical_provenance (
                    instrument TEXT NOT NULL,
                    interval_us INTEGER NOT NULL,
                    opened_us INTEGER NOT NULL,
                    raw_digest TEXT NOT NULL REFERENCES historical_raw(digest),
                    PRIMARY KEY (instrument, interval_us, opened_us, raw_digest),
                    FOREIGN KEY (instrument, interval_us, opened_us)
                        REFERENCES historical_candles(instrument, interval_us, opened_us)
                );
            """)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        try:
            db.execute("PRAGMA foreign_keys = ON")
            with db:
                yield db
        finally:
            db.close()

    def record(self, candles: Iterable[Candle], *, available_at: datetime, raw_payload: bytes, source: str) -> int:
        """Persist a source batch atomically; return newly inserted candle count.

        Use actual receipt/publication time, never infer it from a candle close.
        The caller supplies market data only, without HTTP headers/credentials.
        """
        available_us = _micros(available_at)
        if not source or not isinstance(raw_payload, bytes):
            raise ValueError("Raw market bytes and source label are required")
        bars = ordered_candles(candles)
        if any(_micros(c.closed_at) > available_us for c in bars):
            raise ValueError("Candle cannot be available before it closes")
        digest = hashlib.sha256(source.encode() + b"\0" + raw_payload).hexdigest()
        inserted = 0
        with self._connect() as db:
            db.execute(
                "INSERT OR IGNORE INTO historical_raw VALUES (?, ?, ?, ?)", (digest, source, available_us, raw_payload)
            )
            for candle in bars:
                identity = _identity(candle.instrument)
                interval_us = _interval_us(candle.closed_at - candle.opened_at)
                opened_us = _micros(candle.opened_at)
                payload = json.dumps(candle.to_json_dict(), sort_keys=True, separators=(",", ":"))
                key = (identity, interval_us, opened_us)
                existing = db.execute(
                    "SELECT payload, available_us FROM historical_candles "
                    "WHERE instrument=? AND interval_us=? AND opened_us=?",
                    key,
                ).fetchone()
                if existing:
                    if existing[0] != payload:
                        raise ValueError("Conflicting historical candle; batch rejected")
                    if available_us < existing[1]:
                        raise ValueError("Cannot backdate historical availability")
                else:
                    db.execute(
                        "INSERT INTO historical_candles VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            identity,
                            candle.opened_at.astimezone(IST).date().isoformat(),
                            interval_us,
                            opened_us,
                            _micros(candle.closed_at),
                            available_us,
                            payload,
                        ),
                    )
                    inserted += 1
                db.execute("INSERT OR IGNORE INTO historical_provenance VALUES (?, ?, ?, ?)", (*key, digest))
        return inserted

    def import_jsonl(self, path: str | Path, *, available_at: datetime, source: str) -> int:
        """Import canonical archive exports using an explicitly known availability time."""
        body = Path(path).read_bytes()
        candles = [Candle.from_json_dict(json.loads(line)) for line in body.splitlines() if line.strip()]
        return self.record(candles, available_at=available_at, raw_payload=body, source=source)

    def candles(
        self,
        instrument: Instrument,
        start: datetime,
        end: datetime,
        *,
        as_of: datetime,
        interval: timedelta = timedelta(minutes=1),
    ) -> tuple[Candle, ...]:
        """Completed bars opened in [start,end), with close AND availability <= as_of."""
        start_us, end_us, cutoff = _micros(start), _micros(end), _micros(as_of)
        duration = _interval_us(interval)
        if start_us >= end_us:
            raise ValueError("Historical range must be ordered")
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT payload FROM historical_candles
                WHERE instrument=? AND interval_us=? AND opened_us>=? AND opened_us<?
                    AND closed_us<=? AND closed_us<=? AND available_us<=?
                ORDER BY opened_us
            """,
                (_identity(instrument), duration, start_us, end_us, end_us, cutoff, cutoff),
            ).fetchall()
        return tuple(Candle.from_json_dict(json.loads(row[0])) for row in rows)

    def provenance(self, candles: Iterable[Candle], *, as_of: datetime) -> tuple[str, ...]:
        """Resolve stored, available inputs to raw batch hashes; reject unbacked snapshots."""
        cutoff = _micros(as_of)
        references = set()
        with self._connect() as db:
            for candle in candles:
                key = (
                    _identity(candle.instrument),
                    _interval_us(candle.closed_at - candle.opened_at),
                    _micros(candle.opened_at),
                )
                stored = db.execute(
                    "SELECT payload, available_us FROM historical_candles "
                    "WHERE instrument=? AND interval_us=? AND opened_us=?",
                    key,
                ).fetchone()
                if not stored or stored[1] > cutoff or candle.closed_at > utc(as_of):
                    raise ValueError("Decision input is not available in historical storage")
                if Candle.from_json_dict(json.loads(stored[0])) != candle:
                    raise ValueError("Decision input differs from stored observation")
                rows = db.execute(
                    "SELECT p.raw_digest FROM historical_provenance p "
                    "JOIN historical_raw r ON r.digest=p.raw_digest "
                    "WHERE p.instrument=? AND p.interval_us=? AND p.opened_us=? AND r.received_us<=?",
                    (*key, cutoff),
                ).fetchall()
                if not rows:
                    raise ValueError("Missing raw provenance for decision input")
                references.update(row[0] for row in rows)
        return tuple(sorted(references))


@dataclass(frozen=True)
class HistoryQuality:
    missing_opens: tuple[datetime, ...] = ()
    unexpected_opens: tuple[datetime, ...] = ()
    reasons: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.reasons


def history_quality(
    candles: Iterable[Candle],
    calendar: TradingCalendar,
    start: datetime,
    end: datetime,
    *,
    as_of: datetime,
    interval: timedelta = timedelta(minutes=1),
) -> HistoryQuality:
    """Report only gaps whose expected close is already due at the supplied clock.

    Calendar omissions mean no declared session, not a weekday-based guess.
    """
    start, end, as_of = utc(start), utc(end), utc(as_of)
    _interval_us(interval)
    if start >= end:
        raise ValueError("Quality range must be ordered")
    cutoff = min(end, as_of)
    expected = set()
    sessions = 0
    reasons = set()
    for session in calendar.sessions:
        opened, closed = session.bounds()
        if opened >= end or closed <= start:
            continue
        sessions += 1
        if (closed - opened) % interval:
            reasons.add("SESSION_TIMEFRAME_MISMATCH")
        cursor = opened
        while cursor + interval <= min(closed, cutoff):
            if cursor >= start:
                expected.add(cursor)
            cursor += interval
    if not sessions:
        reasons.add("NO_DECLARED_SESSIONS")
    try:
        bars = ordered_candles(candles)
    except ValueError:
        return HistoryQuality(reasons=("CONFLICTING_DUPLICATE",))
    actual = {c.opened_at for c in bars}
    unexpected = {
        c.opened_at
        for c in bars
        if c.opened_at not in expected or c.closed_at - c.opened_at != interval or c.closed_at > cutoff
    }
    missing = expected - actual
    if missing:
        reasons.add("MISSING_CANDLES")
    if unexpected:
        reasons.add("UNEXPECTED_CANDLES")
    return HistoryQuality(tuple(sorted(missing)), tuple(sorted(unexpected)), tuple(sorted(reasons)))


@dataclass(frozen=True)
class ReplayFrame:
    as_of: datetime
    result: IngestResult
    quality: HistoryQuality
    calendar_version: str


class HistoricalReplay:
    """A clock-bound feed: all consumer requests are restricted to the injected clock."""

    def __init__(self, store: HistoricalStore, calendar: TradingCalendar, *, clock: Callable[[], datetime]):
        self._store, self.calendar, self.clock = store, calendar, clock

    def snapshot(
        self, instrument: Instrument, start: datetime, end: datetime, *, interval: timedelta = timedelta(minutes=1)
    ) -> ReplayFrame:
        now = utc(self.clock())
        candles = self._store.candles(instrument, start, end, as_of=now, interval=interval)
        quality = history_quality(candles, self.calendar, start, end, as_of=now, interval=interval)
        reasons = quality.reasons
        if not candles:
            reasons = tuple(sorted(set(reasons) | {"MISSING_CANDLES"}))
        return ReplayFrame(now, IngestResult(candles=candles, reasons=reasons), quality, self.calendar.version)

    def banknifty_candles(self, start: datetime, end: datetime) -> IngestResult:
        """Same method/result contract as GrowwMarketDataService."""
        return self.snapshot(Instrument("BANKNIFTY", segment="INDEX", series="INDEX"), start, end).result


def replay_sessions(
    store: HistoricalStore,
    calendar: TradingCalendar,
    instrument: Instrument,
    start: datetime,
    end: datetime,
    *,
    interval: timedelta = timedelta(minutes=1),
) -> Iterator[ReplayFrame]:
    """Advance at declared session candle closes, emitting gaps as well as observations.

    Each frame contains only that session's prefix. Missing whole sessions still
    emit invalid frames; holidays create no synthetic bars. Late data is visible
    only at the first subsequent replay tick after its recorded availability.
    """
    start, end = utc(start), utc(end)
    _interval_us(interval)
    if start >= end:
        raise ValueError("Replay range must be ordered")
    sessions = [
        session
        for session in sorted(calendar.sessions, key=lambda item: item.day)
        if session.bounds()[0] < end and session.bounds()[1] > start
    ]
    if not sessions:
        raise ValueError("No declared sessions in replay range")
    if any((session.bounds()[1] - session.bounds()[0]) % interval for session in sessions):
        raise ValueError("Replay timeframe must divide each declared session")
    for session in sessions:
        opened, closed = session.bounds()
        cursor = opened + interval
        while cursor <= min(closed, end):
            if cursor > start:
                feed = HistoricalReplay(store, calendar, clock=lambda at=cursor: at)
                yield feed.snapshot(instrument, max(start, opened), cursor, interval=interval)
            cursor += interval
