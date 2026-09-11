"""Deterministic session-based features shared by live inference and replay."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from itertools import pairwise
from statistics import pstdev
from types import MappingProxyType
from typing import Protocol

from .canonical import IST, Candle, Instrument, TradingCalendar, ordered_candles, utc
from .history import history_quality
from .live import IngestResult

FEATURE_VERSION = "session-features-v2"
NAMES = (
    "return_1",
    "momentum_5",
    "ema_9",
    "ema_21",
    "ema_21_distance",
    "ema_spread",
    "ema_9_slope",
    "ema_21_slope",
    "rsi_14",
    "macd",
    "macd_signal",
    "macd_histogram",
    "atr_14",
    "realized_volatility_20",
    "adx_14",
    "vwap_distance",
    "relative_volume_20",
    "range_fraction",
    "compression_20",
    "breakout_high_20",
    "breakout_low_20",
    "return_5m",
    "range_5m",
    "minutes_since_open",
    "session_fraction",
)
DEFAULT_REQUIRED = ("return_1", "ema_9", "ema_21", "rsi_14", "atr_14", "adx_14", "macd_signal")


class DecisionAudit(Protocol):
    def append(self, event_type: str, payload: Mapping) -> object: ...


class CandleFeed(Protocol):
    def banknifty_candles(self, start: datetime, end: datetime) -> IngestResult: ...


def _ema(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    result = [sum(values[:period]) / period]
    for value in values[period:]:
        result.append(result[-1] + 2 / (period + 1) * (value - result[-1]))
    return result


def _wilder(values: list[float], period: int) -> list[float]:
    if len(values) < period:
        return []
    result = [sum(values[:period]) / period]
    for value in values[period:]:
        result.append((result[-1] * (period - 1) + value) / period)
    return result


@dataclass(frozen=True)
class FeatureSnapshot:
    as_of: datetime
    instrument: Instrument
    version: str
    calendar_version: str
    input_digest: str
    values: Mapping[str, float | None]
    unavailable: Mapping[str, str]
    reasons: tuple[str, ...]
    required: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.reasons and all(self.values[name] is not None for name in self.required)

    def to_json_dict(self) -> dict:
        return {
            "as_of": self.as_of.isoformat(),
            "instrument": self.instrument.key,
            "feature_version": self.version,
            "calendar_version": self.calendar_version,
            "input_digest": self.input_digest,
            "values": dict(self.values),
            "unavailable": dict(self.unavailable),
            "reasons": list(self.reasons),
            "required": list(self.required),
            "ready": self.ready,
        }


class FeatureEngine:
    """V1: one-minute candles, reset each session, no implicit imputation."""

    def __init__(self, calendar: TradingCalendar, *, required: tuple[str, ...] = DEFAULT_REQUIRED):
        if not required or len(set(required)) != len(required) or set(required) - set(NAMES):
            raise ValueError("Required features must be a nonempty unique subset of the versioned contract")
        self.calendar, self.required = calendar, tuple(required)

    def from_feed(self, feed: CandleFeed, as_of: datetime) -> FeatureSnapshot:
        now = utc(as_of)
        instrument = Instrument("BANKNIFTY", segment="INDEX", series="INDEX")
        session = self.calendar.session(now.astimezone(IST).date())
        if session is None:
            return self.compute(IngestResult(reasons=("UNKNOWN_OR_CLOSED_SESSION",)), instrument, now)
        start, _ = session.bounds()
        if now <= start:
            return self.compute(IngestResult(reasons=("INSUFFICIENT_HISTORY",)), instrument, now)
        return self.compute(feed.banknifty_candles(start, now), instrument, now)

    def compute(self, observations: IngestResult, instrument: Instrument, as_of: datetime) -> FeatureSnapshot:
        now = utc(as_of)
        values = {name: None for name in NAMES}
        unavailable = dict.fromkeys(NAMES, "INSUFFICIENT_HISTORY")
        reasons = set(observations.reasons)
        session = self.calendar.session(now.astimezone(IST).date())
        bars = ()
        if session is None:
            reasons.add("UNKNOWN_OR_CLOSED_SESSION")
        else:
            start, end = session.bounds()
            if not start < now <= end:
                reasons.add("OUTSIDE_SESSION")
            try:
                # Feed guarantees availability; filter completed current-session bars again defensively.
                bars = ordered_candles(
                    c for c in observations.candles if c.closed_at <= now and c.opened_at >= start and c.opened_at < end
                )
            except ValueError:
                reasons.add("CONFLICTING_DUPLICATE")
            if any(c.instrument != instrument for c in bars):
                reasons.add("WRONG_INSTRUMENT")
            if now > start:
                reasons.update(history_quality(bars, self.calendar, start, now, as_of=now).reasons)
        digest = hashlib.sha256(json.dumps([c.to_json_dict() for c in bars], sort_keys=True).encode()).hexdigest()
        if not bars:
            reasons.add("INSUFFICIENT_HISTORY")
        if not reasons:
            try:
                computed, absent = _calculate(list(bars), session.bounds()[0], session.bounds()[1], now)
                for name, value in computed.items():
                    if not math.isfinite(value):
                        raise ValueError("Non-finite feature")
                    values[name] = round(value, 10)
                    unavailable.pop(name, None)
                unavailable.update(absent)
            except (ValueError, OverflowError, ZeroDivisionError):
                reasons.add("INVALID_NUMERICAL_INPUT")
                values = dict.fromkeys(NAMES)
                unavailable = dict.fromkeys(NAMES, "INVALID_NUMERICAL_INPUT")
        else:
            unavailable = dict.fromkeys(NAMES, "INVALID_MARKET_STATE")
        return FeatureSnapshot(
            now,
            instrument,
            FEATURE_VERSION,
            self.calendar.version,
            digest,
            MappingProxyType(values),
            MappingProxyType(unavailable),
            tuple(sorted(reasons)),
            self.required,
        )

    def decide(
        self,
        snapshot: FeatureSnapshot,
        *,
        model_id: str,
        predict: Callable[[Mapping[str, float | None]], Mapping],
        audit: DecisionAudit,
    ) -> dict:
        """Audited V2 inference boundary; a failed audit prevents returning a decision."""
        if snapshot.version != FEATURE_VERSION or snapshot.calendar_version != self.calendar.version:
            raise ValueError("Feature contract mismatch")
        if snapshot.required != self.required or not model_id:
            raise ValueError("Model feature requirements or model identity mismatch")
        if snapshot.ready:
            decision = dict(predict(snapshot.values))
        else:
            decision = {"action": "NO_TRADE", "reason": "FEATURES_UNAVAILABLE"}
        # Validate JSON output before recording; no default=str conversions for model values.
        json.dumps(decision, allow_nan=False)
        audit.append(
            "feature_model_decision", {"model_id": model_id, "features": snapshot.to_json_dict(), "decision": decision}
        )
        return decision


def _calculate(bars: list[Candle], start: datetime, end: datetime, now: datetime) -> tuple[dict, dict]:
    close = [float(c.close) for c in bars]
    high, low = [float(c.high) for c in bars], [float(c.low) for c in bars]
    if any(not math.isfinite(v) or v <= 0 for values in (close, high, low) for v in values):
        raise ValueError("Prices outside float range")
    n = len(bars)
    result = {
        "range_fraction": (high[-1] - low[-1]) / close[-1],
        "minutes_since_open": (now - start).total_seconds() / 60,
        "session_fraction": (now - start) / (end - start),
    }
    absent = {}
    if n >= 2:
        result["return_1"] = close[-1] / close[-2] - 1
    if n >= 6:
        result["momentum_5"] = close[-1] / close[-6] - 1
    for period in (9, 21):
        values = _ema(close, period)
        if values:
            result[f"ema_{period}"] = values[-1]
        if len(values) >= 2:
            result[f"ema_{period}_slope"] = values[-1] / values[-2] - 1
    if n >= 21:
        result["ema_21_distance"] = float(bars[-1].close / Decimal(str(result["ema_21"])) - 1)
        result["ema_spread"] = (result["ema_9"] - result["ema_21"]) / close[-1]
        returns = [math.log(b / a) for a, b in zip(close[-21:-1], close[-20:])]
        result["realized_volatility_20"] = pstdev(returns)
        previous_ranges = [h - l for h, l in zip(high[-21:-1], low[-21:-1])]
        baseline = sum(previous_ranges) / 20
        if baseline > 0:
            result["compression_20"] = (high[-1] - low[-1]) / baseline
        else:
            absent["compression_20"] = "ZERO_BASELINE_RANGE"
        result["breakout_high_20"] = close[-1] / max(high[-21:-1]) - 1
        result["breakout_low_20"] = close[-1] / min(low[-21:-1]) - 1
    changes = [b - a for a, b in pairwise(close)]
    gains, losses = _wilder([max(v, 0) for v in changes], 14), _wilder([max(-v, 0) for v in changes], 14)
    if gains:
        gain, loss = gains[-1], losses[-1]
        result["rsi_14"] = 50 if gain + loss == 0 else 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    true_ranges = [max(high[i] - low[i], abs(high[i] - close[i - 1]), abs(low[i] - close[i - 1])) for i in range(1, n)]
    atr = _wilder(true_ranges, 14)
    if atr:
        result["atr_14"] = atr[-1]
        up = [high[i] - high[i - 1] for i in range(1, n)]
        down = [low[i - 1] - low[i] for i in range(1, n)]
        plus = _wilder([u if u > d and u > 0 else 0 for u, d in zip(up, down)], 14)
        minus = _wilder([d if d > u and d > 0 else 0 for u, d in zip(up, down)], 14)
        dx = [100 * abs(p - m) / (p + m) if p + m else 0 for p, m in zip(plus, minus)]
        adx = _wilder(dx, 14)
        if adx:
            result["adx_14"] = adx[-1]
    fast, slow = _ema(close, 12), _ema(close, 26)
    macd = [f - s for f, s in zip(fast[14:], slow)]
    if macd:
        result["macd"] = macd[-1]
        signal = _ema(macd, 9)
        if signal:
            result["macd_signal"] = signal[-1]
            result["macd_histogram"] = macd[-1] - signal[-1]
    volumes = [c.volume for c in bars]
    if any(v is None for v in volumes):
        absent["vwap_distance"] = "MISSING_VOLUME"
    elif sum(volumes) == 0:
        absent["vwap_distance"] = "ZERO_VOLUME"
    else:
        vwap = sum((h + l + c) / 3 * v for h, l, c, v in zip(high, low, close, volumes)) / sum(volumes)
        result["vwap_distance"] = close[-1] / vwap - 1
    if any(v is None for v in volumes[-21:]):
        absent["relative_volume_20"] = "MISSING_VOLUME"
    elif n >= 21:
        baseline = sum(volumes[-21:-1]) / 20
        if baseline:
            result["relative_volume_20"] = volumes[-1] / baseline
        else:
            absent["relative_volume_20"] = "ZERO_VOLUME"
    # Session-aligned complete five-minute groups only; forming groups are excluded.
    groups = [bars[i : i + 5] for i in range(0, n - 4, 5)]
    if groups:
        latest = groups[-1]
        result["range_5m"] = float(max(c.high for c in latest) - min(c.low for c in latest)) / float(latest[-1].close)
    if len(groups) >= 2:
        result["return_5m"] = float(groups[-1][-1].close / groups[-2][-1].close - 1)
    return result, absent
