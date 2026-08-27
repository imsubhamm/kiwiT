"""Read-only Bank Nifty contracts and executable quote validation."""

from __future__ import annotations

import csv
import io
import urllib.request
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from .chart_analysis import VERSION, analyse, history_context, parse_minutes
from .intraday import IST, _quote_time


def positive(value):
    number = Decimal(str(value))
    if not number.is_finite() or number <= 0:
        raise ValueError("Missing positive market value")
    return number


def contracts_from_csv(text: str, today: date) -> list[dict]:
    contracts = []
    for row in csv.DictReader(io.StringIO(text)):
        if (row["exchange"], row["segment"], row["underlying_symbol"]) != ("NSE", "FNO", "BANKNIFTY"):
            continue
        if row["instrument_type"] not in ("CE", "PE"):
            continue
        expiry = date.fromisoformat(row["expiry_date"])
        if expiry <= today:  # initial pilot deliberately excludes expiry day
            continue
        if row["buy_allowed"] != "1" or row["sell_allowed"] != "1" or row["is_reserved"] != "0":
            continue
        lot, freeze = int(row["lot_size"]), int(row["freeze_quantity"])
        if lot <= 0 or freeze <= lot:
            continue
        contracts.append(
            {
                "symbol": row["trading_symbol"],
                "expiry": str(expiry),
                "kind": row["instrument_type"],
                "strike": str(positive(row["strike_price"])),
                "lot": lot,
                "tick": str(positive(row["tick_size"])),
                "freeze": freeze,
            }
        )
    if not contracts:
        raise ValueError("No eligible non-expiry-day Bank Nifty contracts")
    nearest = min(c["expiry"] for c in contracts)
    return [c for c in contracts if c["expiry"] == nearest]


def executable_quote(payload: dict, now: datetime, *, entry: bool = True) -> dict:
    stamp = _quote_time(payload, now)
    if not 0 <= (now - stamp).total_seconds() <= 60:
        raise ValueError("Option quote stale or future-dated")
    bid = positive(payload.get("bid_price"))
    bid_size = int(payload.get("bid_quantity", 0))
    if bid_size <= 0:
        raise ValueError("No bid liquidity")
    quote = {"stamp": stamp.isoformat(), "bid": str(bid), "bid_size": bid_size}
    if entry:
        ask = positive(payload.get("offer_price"))
        ask_size = int(payload.get("offer_quantity", 0))
        if ask < bid or (ask - bid) / ask > Decimal(".02") or ask_size <= 0:
            raise ValueError("Crossed, illiquid or wide-spread quote")
        quote.update(ask=str(ask), ask_size=ask_size)
    return quote


class BankNiftyMarket:
    def __init__(self, broker, clock=None):
        self.broker = broker
        self.clock = clock or (lambda: datetime.now(UTC))

    def quote(self, symbol, now, *, entry=True):
        payload = self.broker.quote(symbol, segment="FNO")
        # Validate against receipt-time clock, not a timestamp captured before network I/O.
        return executable_quote(payload, self.clock(), entry=entry)

    def latest_underlying(self, now):
        bars = parse_minutes(self.broker.banknifty_candles(now - timedelta(minutes=5), now), now)
        if not bars or not 0 <= (now - datetime.fromisoformat(bars[-1]["at"])).total_seconds() <= 120:
            raise ValueError("Underlying recheck candles missing or stale")
        return {"at": bars[-1]["at"], "spot": str(bars[-1]["close"])}

    def snapshot(self, now, cached_context=None):
        local = now.astimezone(IST)
        start = local.replace(hour=9, minute=15, second=0, microsecond=0)
        current = parse_minutes(self.broker.banknifty_candles(start, now), now)
        context = cached_context
        if not context or context.get("day") != str(local.date()) or context.get("version") != VERSION:
            midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
            context = history_context(self.broker.banknifty_candles(midnight - timedelta(days=14), midnight), now)
        analysis = analyse(current, context, now)
        history = [
            {
                "at": b["at"],
                "spot": str(b["close"]),
                "open": str(b["open"]),
                "high": str(b["high"]),
                "low": str(b["low"]),
            }
            for b in current
            if b["at"][:10] == str(local.date())
        ][-20:]
        stamp = datetime.fromisoformat(history[-1]["at"])
        spot = positive(history[-1]["spot"])
        # Public instrument master: no broker credentials sent to the asset host.
        # Literal HTTPS URL only; no caller-controlled schemes or paths.
        with urllib.request.urlopen(  # nosec B310
            "https://growwapi-assets.groww.in/instruments/instrument.csv", timeout=15
        ) as response:
            body = response.read(30_000_001)
        if len(body) > 30_000_000:
            raise ValueError("Instrument master exceeds size limit")
        contracts = contracts_from_csv(body.decode("utf-8-sig"), now.astimezone(IST).date())
        strikes = sorted({Decimal(c["strike"]) for c in contracts}, key=lambda strike: abs(strike - spot))[:3]
        candidates = []
        for contract in contracts:
            if Decimal(contract["strike"]) in strikes:
                try:
                    quote = self.quote(contract["symbol"], now)
                    if min(quote["bid_size"], quote["ask_size"]) >= contract["lot"]:
                        candidates.append(dict(contract, quote=quote))
                except (ValueError, ArithmeticError):
                    continue
        return {
            "at": now.isoformat(),
            "spot": str(spot),
            "spot_at": stamp.isoformat(),
            "candidates": candidates,
            "underlying_history": history,
            "underlying_source": "Groww completed 1-minute index candles",
            "chart_analysis": analysis,
            "chart_cache": context,
        }


def completed_candles(payload, now):
    """Use candle close time, never receipt time; exclude forming/future candles."""
    samples = {}
    if payload.get("interval_in_minutes") != 1:
        raise ValueError("Expected one-minute Bank Nifty candles")
    for row in payload.get("candles", []):
        opened = _quote_time({"timestamp": row[0]}, now)
        closed = opened + timedelta(minutes=1)
        if closed > now or opened.astimezone(IST).date() != now.astimezone(IST).date():
            continue
        op, high, low, close = map(positive, row[1:5])
        if not low <= min(op, close) <= max(op, close) <= high:
            raise ValueError("Invalid underlying candle OHLC")
        samples[closed] = {
            "at": closed.isoformat(),
            "spot": str(close),
            "open": str(op),
            "high": str(high),
            "low": str(low),
        }
    times = sorted(samples)[-20:]
    if len(times) < 5 or not 0 <= (now - times[-1]).total_seconds() <= 120:
        raise ValueError("Bank Nifty completed candles missing or stale")
    if any((b - a).total_seconds() != 60 for a, b in zip(times[-5:], times[-4:])):
        raise ValueError("Bank Nifty completed candles have gaps")
    return [samples[at] for at in times]
