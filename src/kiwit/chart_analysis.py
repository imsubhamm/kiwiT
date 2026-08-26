"""Versioned, point-in-time chart evidence. Heuristics, not proven trading edges."""

import itertools
from collections import defaultdict
from datetime import datetime, time, timedelta
from decimal import Decimal
from math import isfinite

from .intraday import IST, _quote_time

VERSION = "banknifty-chart-v2-week"


def number(value):
    value = Decimal(str(value))
    if not value.is_finite() or value <= 0:
        raise ValueError("Invalid chart price")
    result = float(value)
    if not isfinite(result) or result <= 0:
        raise ValueError("Chart price outside numerical range")
    return result


def parse_minutes(payload, now):
    """Provider timestamps denote opens; return closed regular-session bars only."""
    if payload.get("interval_in_minutes") != 1:
        raise ValueError("Chart analysis requires one-minute candles")
    result = {}
    for row in payload.get("candles", []):
        if len(row) < 5:
            raise ValueError("Incomplete chart candle")
        start = _quote_time({"timestamp": row[0]}, now).astimezone(IST)
        end = start + timedelta(minutes=1)
        if end > now or not time(9, 15) <= start.time() < time(15, 30):
            continue
        if start.second or start.microsecond:
            raise ValueError("Unaligned minute candle")
        op, high, low, close = map(number, row[1:5])
        if not low <= min(op, close) <= max(op, close) <= high:
            raise ValueError("Invalid chart OHLC")
        bar = {"at": end.isoformat(), "open": op, "high": high, "low": low, "close": close}
        if end in result and result[end] != bar:
            raise ValueError("Conflicting duplicate chart candle")
        result[end] = bar
    return [result[at] for at in sorted(result)]


def aggregate(bars, minutes):
    """Anchor buckets at 09:15 IST; never fill gaps or include partial buckets."""
    groups = defaultdict(list)
    for bar in bars:
        start = datetime.fromisoformat(bar["at"]).astimezone(IST) - timedelta(minutes=1)
        anchor = start.replace(hour=9, minute=15, second=0, microsecond=0)
        bucket = anchor + timedelta(minutes=int((start - anchor).total_seconds() // 60) // minutes * minutes)
        groups[bucket].append(bar)
    result = []
    for bucket, group in sorted(groups.items()):
        group.sort(key=lambda b: b["at"])
        expected = [bucket + timedelta(minutes=i + 1) for i in range(minutes)]
        if [datetime.fromisoformat(b["at"]) for b in group] != expected:
            continue
        result.append(
            {
                "at": expected[-1].isoformat(),
                "open": group[0]["open"],
                "high": max(b["high"] for b in group),
                "low": min(b["low"] for b in group),
                "close": group[-1]["close"],
            }
        )
    return result


def history_context(payload, now):
    """Cache only prior completed regular sessions; list coverage rather than infer holidays."""
    day = now.astimezone(IST).date()
    bars = [
        b
        for b in parse_minutes(payload, now)
        if day - timedelta(days=14) <= datetime.fromisoformat(b["at"]).date() < day
    ]
    groups = defaultdict(list)
    for bar in bars:
        groups[bar["at"][:10]].append(bar)
    daily, complete, partial = [], [], []
    for date, group in sorted(groups.items()):
        full = aggregate(group, 375)
        if not full:
            partial.append(date)
            continue
        daily.extend(full)
        complete.extend(group)
    calendar_week = previous_calendar_week(daily, partial, now)
    daily = daily[-5:]
    first = daily[0]["at"][:10] if daily else str(day)
    complete = [b for b in complete if b["at"][:10] >= first]
    return {
        "version": VERSION,
        "day": str(day),
        "daily": daily,
        "five": aggregate(complete, 5)[-100:],
        "fifteen": aggregate(complete, 15)[-100:],
        "partial_sessions": [d for d in partial if d >= first],
        "previous_calendar_week": calendar_week,
        "source": "Groww NSE-BANKNIFTY completed regular-session candles",
    }


def previous_calendar_week(daily, partial, now):
    """Previous Monday-Friday in IST, never a rolling five-session substitute."""
    today = now.astimezone(IST).date()
    monday = today - timedelta(days=today.weekday() + 7)
    friday = monday + timedelta(days=4)
    dates = [str(monday + timedelta(days=i)) for i in range(5)]
    selected = sorted((b for b in daily if b["at"][:10] in dates), key=lambda b: b["at"])
    present = {b["at"][:10] for b in selected}
    partial_dates = sorted(set(partial).intersection(dates))
    absent = [d for d in dates if d not in present and d not in partial_dates]
    complete = len(present) == 5 and not partial_dates
    result = {
        "period_start": str(monday),
        "period_end": str(friday),
        "timezone": "Asia/Kolkata",
        "basis": "Previous calendar week, Monday-Friday; not rolling sessions",
        "coverage": {
            "status": "complete" if complete else "incomplete",
            "complete_sessions": sorted(present),
            "partial_sessions": partial_dates,
            "absent_weekdays_unverified": absent,
            "calendar_verified": False,
        },
        "daily_bars": selected,
        "trend": "insufficient_data",
        "ohlc": None,
        "return_pct": None,
        "range_pct": None,
        "structure": None,
        "summary": "Previous-week coverage incomplete; no full-week trend inferred",
    }
    if not complete:
        return result
    op, close = selected[0]["open"], selected[-1]["close"]
    high, low = max(b["high"] for b in selected), min(b["low"] for b in selected)
    pairs = list(itertools.pairwise(selected))
    structure = {
        name: sum(predicate(a, b) for a, b in pairs)
        for name, predicate in (
            ("higher_closes", lambda a, b: b["close"] > a["close"]),
            ("lower_closes", lambda a, b: b["close"] < a["close"]),
            ("higher_highs", lambda a, b: b["high"] > a["high"]),
            ("higher_lows", lambda a, b: b["low"] > a["low"]),
            ("lower_highs", lambda a, b: b["high"] < a["high"]),
            ("lower_lows", lambda a, b: b["low"] < a["low"]),
        )
    }
    trend = "mixed_or_range"
    if close - op > 0.2 * (high - low) and structure["higher_closes"] >= 3:
        trend = "upward_bias"
    elif op - close > 0.2 * (high - low) and structure["lower_closes"] >= 3:
        trend = "downward_bias"
    result.update(
        trend=trend,
        ohlc={"open": op, "high": high, "low": low, "close": close},
        return_pct=rounded((close / op - 1) * 100),
        range_pct=rounded((high - low) / op * 100),
        structure=structure,
        summary=f"{monday} to {friday}: {trend}; five complete regular sessions. Heuristic context, not a trade signal.",
    )
    return result


def ema(values, period):
    if len(values) < period:
        return None
    value = sum(values[:period]) / period
    for close in values[period:]:
        value += 2 / (period + 1) * (close - value)
    return value


def indicators(bars):
    close = [b["close"] for b in bars]
    fast, slow = ema(close, 9), ema(close, 21)
    atr = rsi = None
    if len(bars) >= 15:
        tail = bars[-15:]
        atr = (
            sum(
                max(b["high"] - b["low"], abs(b["high"] - p["close"]), abs(b["low"] - p["close"]))
                for p, b in itertools.pairwise(tail)
            )
            / 14
        )
        changes = [b["close"] - a["close"] for a, b in itertools.pairwise(tail)]
        gains, losses = sum(max(x, 0) for x in changes), sum(max(-x, 0) for x in changes)
        rsi = 50 if gains + losses == 0 else 100 if losses == 0 else 100 - 100 / (1 + gains / losses)
    regime = "insufficient"
    if fast is not None and slow is not None and atr is not None:
        regime = "range" if abs(fast - slow) <= 0.25 * atr else "uptrend" if fast > slow else "downtrend"
    return {
        "bars": len(bars),
        "at": bars[-1]["at"] if bars else None,
        "regime": regime,
        "ema9": rounded(fast),
        "ema21": rounded(slow),
        "atr14": rounded(atr),
        "rsi14": rounded(rsi),
    }


def rounded(value):
    return round(value, 4) if value is not None else None


def detect_patterns(bars, metrics, opening, previous):
    """Five-minute setups: fixed rules, explicit invalidation, no fitted confidence."""
    if len(bars) < 22:
        return []
    last, prev = bars[-1], bars[-2]
    # Never use yesterday's last candle as today's two-candle setup.
    same_day = last["at"][:10] == prev["at"][:10]
    atr = metrics["atr14"] or 0
    tolerance = atr * 0.1
    result = []

    def add(name, direction, strategy, level, condition, invalidation):
        if condition:
            result.append(
                {
                    "id": name + "_" + direction,
                    "name": name,
                    "direction": direction,
                    "strategy": strategy,
                    "timeframe": "5m",
                    "at": last["at"],
                    "level": rounded(level),
                    "invalidation": rounded(invalidation),
                    "observed_close": last["close"],
                }
            )

    if same_day:
        for label, levels in (("opening_range", opening), ("previous_day", previous)):
            if levels:
                for direction, field in (("bullish", "high"), ("bearish", "low")):
                    level = levels[field]
                    condition = (
                        prev["close"] <= level < last["close"]
                        if direction == "bullish"
                        else prev["close"] >= level > last["close"]
                    )
                    add(label + "_breakout", direction, "momentum", level, condition, level)
        base = bars[-22:-2]
        high, low = max(b["high"] for b in base), min(b["low"] for b in base)
        add(
            "breakout_retest",
            "bullish",
            "momentum",
            high,
            prev["close"] > high and last["low"] <= high + tolerance and last["close"] > high,
            high,
        )
        add(
            "breakout_retest",
            "bearish",
            "momentum",
            low,
            prev["close"] < low and last["high"] >= low - tolerance and last["close"] < low,
            low,
        )
        add(
            "engulfing",
            "bullish",
            "reversal",
            last["close"],
            prev["close"] < prev["open"]
            and last["close"] > last["open"]
            and last["open"] <= prev["close"]
            and last["close"] >= prev["open"],
            min(prev["low"], last["low"]),
        )
        add(
            "engulfing",
            "bearish",
            "reversal",
            last["close"],
            prev["close"] > prev["open"]
            and last["close"] < last["open"]
            and last["open"] >= prev["close"]
            and last["close"] <= prev["open"],
            max(prev["high"], last["high"]),
        )
    fast = metrics["ema9"]
    if fast is not None:
        add(
            "ema_pullback",
            "bullish",
            "momentum",
            fast,
            metrics["regime"] == "uptrend" and last["low"] <= fast < last["close"] and last["close"] > last["open"],
            last["low"],
        )
        add(
            "ema_pullback",
            "bearish",
            "momentum",
            fast,
            metrics["regime"] == "downtrend" and last["high"] >= fast > last["close"] and last["close"] < last["open"],
            last["high"],
        )
    body = abs(last["close"] - last["open"])
    lower, upper = min(last["open"], last["close"]) - last["low"], last["high"] - max(last["open"], last["close"])
    add("hammer", "bullish", "reversal", last["close"], body > 0 and lower >= 2 * body and upper <= body, last["low"])
    add(
        "shooting_star",
        "bearish",
        "reversal",
        last["close"],
        body > 0 and upper >= 2 * body and lower <= body,
        last["high"],
    )
    prior = bars[-21:-1]
    high, low = max(b["high"] for b in prior), min(b["low"] for b in prior)
    add(
        "range_rejection",
        "bullish",
        "reversal",
        low,
        metrics["regime"] == "range"
        and last["low"] <= low + tolerance
        and low < last["close"] <= high
        and last["close"] > last["open"],
        last["low"],
    )
    add(
        "range_rejection",
        "bearish",
        "reversal",
        high,
        metrics["regime"] == "range"
        and last["high"] >= high - tolerance
        and low <= last["close"] < high
        and last["close"] < last["open"],
        last["high"],
    )
    return result


def analyse(current, context, now):
    day = str(now.astimezone(IST).date())
    current = [b for b in current if b["at"][:10] == day and datetime.fromisoformat(b["at"]) <= now]
    if len(current) < 5 or not 0 <= (now - datetime.fromisoformat(current[-1]["at"])).total_seconds() <= 120:
        raise ValueError("Chart candles missing or stale")
    anchor = now.astimezone(IST).replace(hour=9, minute=15, second=0, microsecond=0)
    expected = int((datetime.fromisoformat(current[-1]["at"]) - anchor).total_seconds() // 60)
    issues = []
    if len(current) != expected:
        issues.append("Current session has missing minute candles")
    if context.get("day") != day or context.get("version") != VERSION:
        raise ValueError("Historical chart cache belongs to another day/version")
    daily = context["daily"]
    if len(daily) < 5:
        issues.append("Fewer than five complete prior regular sessions")
    if context["partial_sessions"]:
        issues.append("Incomplete prior sessions: " + ", ".join(context["partial_sessions"]))
    calendar_week = context.get("previous_calendar_week")
    if not calendar_week or calendar_week["coverage"]["status"] != "complete":
        issues.append("Previous calendar week incomplete; absent weekdays require data or holiday verification")
    five_today, fifteen_today = aggregate(current, 5), aggregate(current, 15)
    five = (context["five"] + five_today)[-100:]
    fifteen = (context["fifteen"] + fifteen_today)[-100:]
    frames = {"1m": indicators(current[-100:]), "5m": indicators(five), "15m": indicators(fifteen)}
    if not fifteen_today or frames["15m"]["regime"] == "insufficient":
        issues.append("Waiting for completed 15-minute context")
    opening = (
        fifteen_today[0]
        if fifteen_today and fifteen_today[0]["at"] == anchor.replace(hour=9, minute=30).isoformat()
        else None
    )
    previous = daily[-1] if daily else None
    patterns = detect_patterns(five, frames["5m"], opening, previous) if five_today else []
    spot = current[-1]["close"]
    patterns = [
        p
        for p in patterns
        if 0 <= (now - datetime.fromisoformat(p["at"])).total_seconds() <= 300
        and (spot > p["invalidation"] if p["direction"] == "bullish" else spot < p["invalidation"])
    ]
    weekly = None
    if daily:
        weekly = {
            "sessions": [b["at"][:10] for b in daily],
            "high": max(b["high"] for b in daily),
            "low": min(b["low"] for b in daily),
            "return_pct": rounded((daily[-1]["close"] / daily[0]["open"] - 1) * 100),
            "basis": "Last five complete observed sessions, not calendar week",
        }
    return {
        "version": VERSION,
        "at": current[-1]["at"],
        "ready": not issues,
        "issues": issues,
        "timeframes": frames,
        "previous_sessions": daily,
        "week": weekly,
        "previous_calendar_week": calendar_week,
        "weekly_alignment": weekly_alignment(calendar_week, frames["15m"]["regime"], spot),
        "previous_day": previous,
        "opening_range": opening,
        "patterns": patterns,
        "gap_pct": rounded((current[0]["open"] / previous["close"] - 1) * 100) if previous else None,
        "summary": "; ".join(issues)
        if issues
        else f"15m {frames['15m']['regime']}; {len(patterns)} active rule-based setups",
        "limitations": [
            "Heuristic patterns, not validated edges or win probabilities",
            "Index has no traded volume: no VWAP or volume confirmation",
            "Absent sessions are not independently verified against an exchange calendar",
        ],
        "chart_bars": {"1m": current[-60:], "5m": five[-40:], "15m": fifteen[-30:]},
    }


def weekly_alignment(week, intraday_regime, spot):
    if not week or week["coverage"]["status"] != "complete":
        return {"alignment": "unknown", "price_location": "unknown"}
    trend = week["trend"]
    aligned = (trend, intraday_regime) in {("upward_bias", "uptrend"), ("downward_bias", "downtrend")}
    conflicting = (trend, intraday_regime) in {("upward_bias", "downtrend"), ("downward_bias", "uptrend")}
    high, low = week["ohlc"]["high"], week["ohlc"]["low"]
    return {
        "alignment": "aligned" if aligned else "conflicting" if conflicting else "mixed",
        "price_location": "above_previous_week_high"
        if spot > high
        else "below_previous_week_low"
        if spot < low
        else "inside_previous_week_range",
        "distance_to_high_pct": rounded((spot / high - 1) * 100),
        "distance_to_low_pct": rounded((spot / low - 1) * 100),
    }


def entry_evidence(analysis, kind, strategy, now):
    if not analysis or not analysis.get("ready") or analysis.get("version") != VERSION:
        return []
    if not 0 <= (now - datetime.fromisoformat(analysis["at"])).total_seconds() <= 120:
        return []
    direction = {"CE": "bullish", "PE": "bearish"}.get(kind)
    return [
        p
        for p in analysis["patterns"]
        if p["direction"] == direction
        and p["strategy"] == strategy
        and 0 <= (now - datetime.fromisoformat(p["at"])).total_seconds() <= 300
    ]
