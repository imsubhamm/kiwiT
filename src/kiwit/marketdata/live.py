"""Read-only Groww ingestion with bounded retries and durable canonical observations."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import urllib.error
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import DecimalException
from pathlib import Path
from typing import TYPE_CHECKING

from kiwit.brokers.groww import BrokerApiError, GrowwBrokerClient, GrowwSettings, _urlopen_transport

from .canonical import (
    IST,
    Candle,
    Instrument,
    Quote,
    TradingCalendar,
    assess_market,
    exchange_timestamp,
    groww_candles,
    utc,
)

if TYPE_CHECKING:
    from .history import HistoricalStore


class MarketDataUnavailable(RuntimeError):
    """Only stable reason codes cross this service's error boundary."""


class ReadRetryTransport:
    """Throttle requests; retry transient GET failures, never order/token mutations."""

    def __init__(
        self,
        transport=_urlopen_transport,
        *,
        attempts=3,
        minimum_interval=0.25,
        backoff=0.5,
        sleep=time.sleep,
        monotonic=time.monotonic,
    ):
        if type(attempts) is not int or not 1 <= attempts <= 5:
            raise ValueError("Retry attempts must be between one and five")
        if not 0 <= minimum_interval <= 60 or not 0 <= backoff <= 10:
            raise ValueError("Invalid rate limit/backoff")
        self.transport, self.attempts = transport, attempts
        self.minimum_interval, self.backoff = minimum_interval, backoff
        self.sleep, self.monotonic = sleep, monotonic
        self._last = None
        self._lock = threading.Lock()

    def __call__(self, request, timeout):
        attempts = self.attempts if request.get_method() == "GET" else 1
        for attempt in range(attempts):
            with self._lock:
                now = self.monotonic()
                if self._last is not None:
                    self.sleep(max(0, self.minimum_interval - (now - self._last)))
                self._last = self.monotonic()
                try:
                    status, body = self.transport(request, timeout)
                except urllib.error.HTTPError as error:
                    if error.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                        raise
                except (TimeoutError, ConnectionError, urllib.error.URLError):
                    if attempt == attempts - 1:
                        raise
                else:
                    if status not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                        return status, body
            self.sleep(self.backoff * 2**attempt)
        raise RuntimeError("Unreachable retry state")


class ObservationStore:
    """Content-addressed SQLite records; retries cannot append duplicate observations."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS market_observations "
                "(digest TEXT PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL)"
            )

    def append(self, records: list[dict], kind: str):
        with sqlite3.connect(self.path) as db:
            for record in records:
                payload = json.dumps(record, sort_keys=True, separators=(",", ":"))
                digest = hashlib.sha256((kind + payload).encode()).hexdigest()
                db.execute("INSERT OR IGNORE INTO market_observations VALUES (?, ?, ?)", (digest, kind, payload))


@dataclass(frozen=True)
class IngestResult:
    candles: tuple[Candle, ...] = ()
    quote: Quote | None = None
    reasons: tuple[str, ...] = ()

    @property
    def invalid_market_state(self):
        return bool(self.reasons)

    def require_valid(self):
        if self.invalid_market_state:
            raise MarketDataUnavailable(",".join(self.reasons)) from None
        return self


def quote_record(quote: Quote) -> dict:
    from dataclasses import asdict

    record = asdict(quote)
    for field in ("exchange_at", "received_at"):
        record[field] = getattr(quote, field).isoformat()
    for field in ("ltp", "bid", "ask"):
        value = getattr(quote, field)
        record[field] = None if value is None else str(value)
    return record


class GrowwMarketDataService:
    """Data-only facade. No order methods or raw payloads exposed to strategies."""

    def __init__(
        self,
        client: GrowwBrokerClient,
        store: ObservationStore,
        calendar: TradingCalendar,
        *,
        clock=lambda: datetime.now(UTC),
        max_age=timedelta(seconds=60),
        history: HistoricalStore | None = None,
    ):
        if max_age < timedelta(0):
            raise ValueError("Negative freshness threshold")
        self._client, self._store = client, store
        self.calendar, self.clock, self.max_age = calendar, clock, max_age
        self._history = history

    @classmethod
    def from_settings(cls, settings: GrowwSettings, store: ObservationStore, calendar: TradingCalendar, **kwargs):
        return cls(GrowwBrokerClient(settings, ReadRetryTransport()), store, calendar, **kwargs)

    def quote(self, instrument: Instrument) -> IngestResult:
        try:
            payload = self._client.quote(instrument.symbol, segment=instrument.segment, exchange=instrument.exchange)
            received = utc(self.clock())
            stamp = next(
                (
                    payload.get(key)
                    for key in ("timestamp", "last_trade_time", "lastTradeTime")
                    if payload.get(key) is not None
                ),
                None,
            )
            quote = Quote(
                instrument,
                exchange_timestamp(stamp),
                received,
                payload.get("last_price"),
                "groww",
                payload.get("bid_price"),
                payload.get("offer_price"),
            )
            reasons = []
            session = self.calendar.session(received.astimezone(IST).date())
            if session is None:
                reasons.append("UNKNOWN_OR_CLOSED_SESSION")
            elif not session.bounds()[0] <= received < session.bounds()[1]:
                reasons.append("OUTSIDE_SESSION")
            if received - quote.exchange_at > self.max_age:
                reasons.append("STALE_QUOTE")
            self._store.append([quote_record(quote)], "quote")
            return IngestResult(quote=quote, reasons=tuple(reasons))
        except (ValueError, TypeError, KeyError, IndexError, DecimalException):
            return IngestResult(reasons=("INVALID_QUOTE",))
        except (BrokerApiError, OSError, sqlite3.Error):
            # No upstream exception, payload or credential is interpolated or logged.
            return IngestResult(reasons=("QUOTE_INGESTION_UNAVAILABLE",))

    def banknifty_candles(self, start: datetime, end: datetime) -> IngestResult:
        start, end = utc(start), utc(end)
        if start >= end:
            raise ValueError("Candle range must be ordered")
        instrument = Instrument("BANKNIFTY", segment="INDEX", series="INDEX")
        try:
            payload = self._client.banknifty_candles(start, end)
            received = utc(self.clock())
            # Never admit observations beyond the requested replay cutoff.
            as_of = min(received, end)
            candles = groww_candles(payload, as_of, instrument, self.calendar)
            candles = tuple(c for c in candles if c.opened_at >= start)
            state = assess_market(candles, instrument, received, self.calendar, max_age=self.max_age)
            if self._history is not None:
                raw_market = {
                    "interval_in_minutes": payload.get("interval_in_minutes"),
                    "candles": payload.get("candles", []),
                }
                self._history.record(
                    candles,
                    available_at=received,
                    raw_payload=json.dumps(raw_market, allow_nan=False).encode(),
                    source="groww",
                )
            self._store.append([c.to_json_dict() for c in candles], "candle")
            return IngestResult(candles=candles, reasons=state.reasons)
        except (ValueError, TypeError, KeyError, IndexError, DecimalException):
            return IngestResult(reasons=("INVALID_CANDLES",))
        except (BrokerApiError, OSError, sqlite3.Error):
            return IngestResult(reasons=("CANDLE_INGESTION_UNAVAILABLE",))
