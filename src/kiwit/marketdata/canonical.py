"""Version 1 market contract shared by archive ingestion and live/replay candles."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
SCHEMA_VERSION = "market-v1"


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Market timestamps must include a timezone")
    return value.astimezone(UTC)


def exchange_timestamp(value) -> datetime:
    """Groww epoch seconds/milliseconds or ISO; naive provider ISO explicitly means IST."""
    if isinstance(value, str) and value.isdigit():
        value = int(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000 if value > 10_000_000_000 else value, UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value)
        return utc(parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed)
    raise ValueError("Missing exchange timestamp")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    exchange: str = "NSE"
    segment: str = "CASH"
    series: str = "EQ"
    isin: str | None = None

    def __post_init__(self):
        if any(
            not isinstance(v, str) or not v.strip() for v in (self.symbol, self.exchange, self.segment, self.series)
        ):
            raise ValueError("Instrument identity fields are required")

    @property
    def key(self) -> str:
        return f"{self.exchange}:{self.segment}:{self.series}:{self.symbol}"


def price(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite() or result <= 0:
        raise ValueError("Market prices must be finite and positive")
    return result


@dataclass(frozen=True)
class Candle:
    instrument: Instrument
    opened_at: datetime
    closed_at: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int | None
    source: str
    source_sha256: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != SCHEMA_VERSION or not self.source:
            raise ValueError("Unsupported schema or missing source")
        for field in ("opened_at", "closed_at"):
            object.__setattr__(self, field, utc(getattr(self, field)))
        if self.closed_at <= self.opened_at:
            raise ValueError("Candle close must follow open")
        for field in ("open", "high", "low", "close"):
            object.__setattr__(self, field, price(getattr(self, field)))
        if not self.low <= min(self.open, self.close) <= max(self.open, self.close) <= self.high:
            raise ValueError("Invalid candle OHLC")
        if self.volume is not None and (type(self.volume) is not int or self.volume < 0):
            raise ValueError("Volume must be a nonnegative integer or unknown")

    def to_json_dict(self) -> dict:
        result = asdict(self)
        for field in ("opened_at", "closed_at"):
            result[field] = getattr(self, field).isoformat()
        for field in ("open", "high", "low", "close"):
            result[field] = str(getattr(self, field))
        return result

    @classmethod
    def from_json_dict(cls, value: dict) -> Candle:
        data = dict(value)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Missing or unsupported schema version")
        data["instrument"] = Instrument(**data["instrument"])
        for field in ("opened_at", "closed_at"):
            data[field] = datetime.fromisoformat(data[field])
        return cls(**data)


@dataclass(frozen=True)
class Quote:
    instrument: Instrument
    exchange_at: datetime
    received_at: datetime
    ltp: Decimal
    source: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if self.schema_version != SCHEMA_VERSION or not self.source:
            raise ValueError("Unsupported schema or missing source")
        for field in ("exchange_at", "received_at"):
            object.__setattr__(self, field, utc(getattr(self, field)))
        object.__setattr__(self, "ltp", price(self.ltp))
        for field in ("bid", "ask"):
            if getattr(self, field) is not None:
                object.__setattr__(self, field, price(getattr(self, field)))
        if self.exchange_at > self.received_at:
            raise ValueError("Quote exchange time exceeds receipt time")
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError("Crossed quote")


@dataclass(frozen=True)
class Session:
    day: date
    opens: time = time(9, 15)
    closes: time = time(15, 30)

    def bounds(self) -> tuple[datetime, datetime]:
        if self.opens.tzinfo or self.closes.tzinfo or self.opens >= self.closes:
            raise ValueError("Session requires ordered local exchange times")
        return tuple(utc(datetime.combine(self.day, t, IST)) for t in (self.opens, self.closes))


@dataclass(frozen=True)
class TradingCalendar:
    """Explicit dated sessions support holidays and special weekend sessions.

    Dates absent from this versioned calendar are unknown/closed, never inferred weekdays.
    """

    version: str
    sessions: tuple[Session, ...]

    def __post_init__(self):
        if not self.version or len({s.day for s in self.sessions}) != len(self.sessions):
            raise ValueError("Calendar needs a version and unique session dates")
        for session in self.sessions:
            session.bounds()

    def session(self, day: date) -> Session | None:
        return next((s for s in self.sessions if s.day == day), None)


def ordered_candles(candles: Iterable[Candle]) -> tuple[Candle, ...]:
    unique = {}
    for candle in candles:
        key = (candle.instrument.key, candle.opened_at, candle.closed_at)
        if key in unique and unique[key] != candle:
            raise ValueError("Conflicting duplicate candle")
        unique[key] = candle
    return tuple(unique[key] for key in sorted(unique))


@dataclass(frozen=True)
class MarketState:
    reasons: tuple[str, ...]

    @property
    def invalid_market_state(self) -> bool:
        return bool(self.reasons)

    @property
    def trade_allowed(self) -> bool:
        return not self.invalid_market_state


def assess_market(
    candles: Iterable[Candle],
    instrument: Instrument,
    now: datetime,
    calendar: TradingCalendar,
    *,
    interval: timedelta = timedelta(minutes=1),
    max_age: timedelta = timedelta(minutes=2),
    quote: Quote | None = None,
    require_quote: bool = False,
) -> MarketState:
    now = utc(now)
    if interval <= timedelta(0) or max_age < timedelta(0):
        raise ValueError("Invalid interval or freshness policy")
    session = calendar.session(now.astimezone(IST).date())
    if session is None:
        return MarketState(("UNKNOWN_OR_CLOSED_SESSION",))
    opened, closed = session.bounds()
    if not opened <= now < closed:
        return MarketState(("OUTSIDE_SESSION",))
    try:
        bars = ordered_candles(c for c in candles if c.instrument == instrument)
    except ValueError:
        return MarketState(("CONFLICTING_DUPLICATE",))
    reasons = set()
    current = [c for c in bars if c.opened_at >= opened and c.opened_at < closed]
    if any(c.closed_at > now for c in current):
        reasons.add("FUTURE_OR_FORMING_CANDLE")
    completed = [c for c in current if c.closed_at <= now]
    if not completed:
        reasons.add("MISSING_CANDLES")
    else:
        if now - completed[-1].closed_at > max_age:
            reasons.add("STALE_CANDLES")
        expected = opened
        for candle in completed:
            if candle.opened_at != expected or candle.closed_at - candle.opened_at != interval:
                reasons.add("MISSING_OR_MISALIGNED_CANDLES")
            expected = candle.closed_at
    if require_quote and quote is None:
        reasons.add("MISSING_QUOTE")
    if quote is not None:
        if quote.instrument != instrument:
            reasons.add("WRONG_QUOTE_INSTRUMENT")
        if quote.exchange_at > now or quote.received_at > now:
            reasons.add("FUTURE_QUOTE")
        elif now - quote.exchange_at > max_age:
            reasons.add("STALE_QUOTE")
    return MarketState(tuple(sorted(reasons)))


def archive_candle(bar, session: Session | None = None) -> Candle:
    """Archive date is a session label, not midnight or a known publication time."""
    session = session or Session(bar.trading_date)
    if session.day != bar.trading_date:
        raise ValueError("Archive session date mismatch")
    opened, closed = session.bounds()
    return Candle(
        Instrument(bar.symbol, series=bar.series, isin=bar.isin),
        opened,
        closed,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.source_format,
        bar.source_sha256,
    )


def groww_candles(
    payload: dict, now: datetime, instrument: Instrument, calendar: TradingCalendar | None = None
) -> tuple[Candle, ...]:
    now = utc(now)
    if payload.get("interval_in_minutes") != 1:
        raise ValueError("Expected one-minute candles")
    result = []
    for row in payload.get("candles", []):
        if len(row) < 5:
            raise ValueError("Incomplete candle")
        opened = exchange_timestamp(row[0])
        closed = opened + timedelta(minutes=1)
        local = opened.astimezone(IST)
        session = calendar.session(local.date()) if calendar else Session(local.date())
        if session is None:
            continue
        session_open, session_close = session.bounds()
        if closed > now or not session_open <= opened < closed <= session_close:
            continue
        if opened.second or opened.microsecond:
            raise ValueError("Unaligned minute candle")
        result.append(Candle(instrument, opened, closed, *row[1:5], row[5] if len(row) > 5 else None, "groww"))
    return ordered_candles(result)
