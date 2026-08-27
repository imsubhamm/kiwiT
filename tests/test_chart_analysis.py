from datetime import datetime, timedelta

import pytest

from kiwit.chart_analysis import (
    VERSION,
    aggregate,
    analyse,
    detect_patterns,
    entry_evidence,
    history_context,
    indicators,
    parse_minutes,
    previous_calendar_week,
    weekly_alignment,
)
from kiwit.intraday import IST
from kiwit.options_ai import request_body


def rows(day, count=375, base=55000):
    start = datetime.fromisoformat(day + "T09:15:00").replace(tzinfo=IST)
    return [
        [(start + timedelta(minutes=i)).isoformat(), base + i, base + i + 2, base + i - 1, base + i + 1, None]
        for i in range(count)
    ]


NOW = datetime(2026, 8, 26, 10, 0, tzinfo=IST)
HISTORY_DATES = ["2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21", "2026-08-24", "2026-08-25"]


def payload(data):
    return {"interval_in_minutes": 1, "candles": data}


def context():
    return history_context(payload([row for d in HISTORY_DATES for row in rows(d)]), NOW)


def test_regular_session_filtering_and_future_exclusion():
    data = rows("2026-08-26", 60)
    data += [["2026-08-26T09:00:00+05:30", 1, 1, 1, 1]]
    result = parse_minutes(payload(data), NOW)
    assert len(result) == 45
    assert result[0]["at"].endswith("09:16:00+05:30")
    assert result[-1]["at"] == NOW.isoformat()
    changed = [r if datetime.fromisoformat(r[0]) < NOW else [r[0], 999, 999, 999, 999] for r in data]
    assert parse_minutes(payload(changed), NOW) == result


def test_calendar_week_is_distinct_from_rolling_sessions_and_excludes_current_week():
    data = [row for i, d in enumerate(HISTORY_DATES[:5]) for row in rows(d, base=55000 + i * 100)]
    data += rows("2026-08-24", base=90000) + rows("2026-08-25", base=91000)
    ctx = history_context(payload(data), NOW)
    week = ctx["previous_calendar_week"]
    assert [b["at"][:10] for b in ctx["daily"]] == HISTORY_DATES[-5:]
    assert week["coverage"]["complete_sessions"] == HISTORY_DATES[:5]
    assert week["ohlc"] == {"open": 55000, "high": 55776, "low": 54999, "close": 55775}
    assert week["return_pct"] == round((55775 / 55000 - 1) * 100, 4)
    assert week["trend"] == "upward_bias"
    result = analyse(parse_minutes(payload(rows("2026-08-26", 45)), NOW), ctx, NOW)
    assert result["previous_calendar_week"] == week
    assert result["weekly_alignment"]["alignment"] in {"aligned", "conflicting", "mixed"}


@pytest.mark.parametrize(
    "at,start,end",
    [
        ("2026-08-24T09:00:00+05:30", "2026-08-17", "2026-08-21"),
        ("2026-08-30T23:00:00+05:30", "2026-08-17", "2026-08-21"),
        ("2026-08-23T20:00:00+00:00", "2026-08-17", "2026-08-21"),
        ("2026-01-01T09:00:00+05:30", "2025-12-22", "2025-12-26"),
    ],
)
def test_previous_calendar_week_boundaries_use_ist(at, start, end):
    week = previous_calendar_week([], [], datetime.fromisoformat(at))
    assert (week["period_start"], week["period_end"]) == (start, end)


@pytest.mark.parametrize("step,trend", [(2, "upward_bias"), (-2, "downward_bias"), (0, "mixed_or_range")])
def test_weekly_trend_and_price_alignment(step, trend):
    daily = [
        {
            "at": d + "T15:30:00+05:30",
            "open": 100 + i * step,
            "high": 101 + i * step,
            "low": 99 + i * step,
            "close": 100 + i * step,
        }
        for i, d in enumerate(HISTORY_DATES[:5])
    ]
    week = previous_calendar_week(daily, [], NOW)
    assert week["trend"] == trend
    assert week["structure"]["higher_closes"] == (4 if step > 0 else 0)
    assert week["structure"]["lower_closes"] == (4 if step < 0 else 0)
    above = weekly_alignment(week, "uptrend", 200)
    assert above["alignment"] == ("aligned" if step > 0 else "conflicting" if step < 0 else "mixed")
    assert above["price_location"] == "above_previous_week_high"
    assert weekly_alignment(week, "downtrend", 50)["price_location"] == "below_previous_week_low"
    assert weekly_alignment(week, "range", 100)["price_location"] == "inside_previous_week_range"


def test_missing_or_partial_week_blocks_entries_without_inventing_holidays():
    data = [row for d in HISTORY_DATES[2:] for row in rows(d)] + rows("2026-08-18", count=374)
    ctx = history_context(payload(data), NOW)
    assert len(ctx["daily"]) == 5  # Rolling coverage alone is no longer sufficient.
    week = ctx["previous_calendar_week"]
    assert week["coverage"]["status"] == "incomplete"
    assert week["coverage"]["absent_weekdays_unverified"] == ["2026-08-17"]
    assert week["coverage"]["partial_sessions"] == ["2026-08-18"]
    assert week["coverage"]["calendar_verified"] is False
    assert week["ohlc"] is None and week["trend"] == "insufficient_data"
    result = analyse(parse_minutes(payload(rows("2026-08-26", 45)), NOW), ctx, NOW)
    assert not result["ready"]
    assert any("calendar week incomplete" in issue for issue in result["issues"])
    assert result["weekly_alignment"] == {"alignment": "unknown", "price_location": "unknown"}


def test_invalid_and_conflicting_duplicates_fail_closed():
    data = rows("2026-08-26", 10)
    assert len(parse_minutes(payload(data + data), NOW)) == 10
    with pytest.raises(ValueError, match="Conflicting"):
        parse_minutes(payload(data + [[data[0][0], 100, 102, 99, 101]]), NOW)
    for values in ([float("nan"), 2, 1, 1], [2, 1, 3, 2], [0, 2, 1, 1], ["1e10000"] * 4, ["1e-10000"] * 4):
        with pytest.raises(ValueError):
            parse_minutes(payload([[data[0][0], *values]]), NOW)


def test_aggregation_is_session_anchored_and_ignores_incomplete_or_gapped_buckets():
    bars = parse_minutes(payload(rows("2026-08-26", 19)), NOW)
    assert [b["at"][11:16] for b in aggregate(bars, 5)] == ["09:20", "09:25", "09:30"]
    assert [b["at"][11:16] for b in aggregate(bars, 15)] == ["09:30"]
    assert aggregate(bars[:4] + bars[5:], 15) == []


def test_prior_context_and_full_analysis_are_bounded_and_point_in_time():
    ctx = context()
    assert len(ctx["daily"]) == 5 and len(ctx["five"]) == len(ctx["fifteen"]) == 100
    analysis = analyse(parse_minutes(payload(rows("2026-08-26", 45)), NOW), ctx, NOW)
    assert analysis["ready"]
    assert analysis["week"]["sessions"][-1] == "2026-08-25"
    assert analysis["previous_day"]["at"][11:16] == "15:30"
    assert analysis["opening_range"]["at"][11:16] == "09:30"
    assert analysis["timeframes"]["15m"]["regime"] != "insufficient"
    snapshot = {"chart_analysis": {k: v for k, v in analysis.items() if k != "chart_bars"}}
    assert len(request_body(snapshot)) < 12000
    assert not entry_evidence(dict(analysis, ready=False), "CE", "momentum", NOW)


def test_gaps_and_insufficient_history_are_visible_and_block_entries():
    ctx = context()
    current = parse_minutes(payload(rows("2026-08-26", 45)), NOW)
    result = analyse(current[:10] + current[11:], ctx, NOW)
    assert not result["ready"] and "missing" in result["issues"][0]
    ctx["daily"] = ctx["daily"][-4:]
    assert not analyse(current, ctx, NOW)["ready"]
    with pytest.raises(ValueError, match="stale"):
        analyse(current, context(), NOW + timedelta(minutes=3))
    with pytest.raises(ValueError, match="another day"):
        analyse(current, dict(context(), day="2026-08-25"), NOW)


def test_partial_history_not_silently_treated_as_full_session():
    data = rows("2026-08-24") + rows("2026-08-25", 374)
    ctx = history_context(payload(data), NOW)
    assert len(ctx["daily"]) == 1
    assert ctx["partial_sessions"] == ["2026-08-25"]


def test_history_outside_requested_window_is_not_used_as_recent_context():
    ctx = history_context(payload(rows("2026-07-01")), NOW)
    assert ctx["daily"] == [] and ctx["five"] == [] and ctx["fifteen"] == []


def test_latest_minute_invalidates_an_older_five_minute_breakout():
    data = [[r[0], 100, 101, 99, 100] for r in rows("2026-08-26", 21)]
    data[19][1:5] = [100, 104, 99, 103]  # 09:35 close above opening range
    at = NOW.replace(hour=9, minute=35)
    before = analyse(parse_minutes(payload(data), at), context(), at)
    assert any(p["id"] == "opening_range_breakout_bullish" for p in before["patterns"])
    at += timedelta(minutes=1)  # next minute closed back below the breakout level
    after = analyse(parse_minutes(payload(data), at), context(), at)
    assert not any(p["id"] == "opening_range_breakout_bullish" for p in after["patterns"])


def test_indicators_flat_market_and_sufficient_warmup():
    bars = parse_minutes(payload([[r[0], 100, 100, 100, 100] for r in rows("2026-08-26", 30)]), NOW)
    metrics = indicators(bars)
    assert metrics["regime"] == "range" and metrics["rsi14"] == 50 and metrics["atr14"] == 0
    assert indicators(bars[:5])["regime"] == "insufficient"
    assert detect_patterns(bars, metrics, None, None) == []


def test_breakout_evidence_direction_invalidation_and_expiry():
    bars = parse_minutes(payload([[r[0], 100, 101, 99, 100] for r in rows("2026-08-26", 30)]), NOW)
    bars[-1].update(open=100, high=104, low=99.5, close=103)
    patterns = detect_patterns(bars, indicators(bars), {"high": 102, "low": 98}, None)
    setup = next(p for p in patterns if p["id"] == "opening_range_breakout_bullish")
    assert setup["invalidation"] == 102 and setup["at"] == bars[-1]["at"]
    now = datetime.fromisoformat(bars[-1]["at"])
    analysis = {"version": VERSION, "at": now.isoformat(), "ready": True, "patterns": [setup]}
    assert entry_evidence(analysis, "CE", "momentum", now)
    assert not entry_evidence(analysis, "PE", "momentum", now)
    assert not entry_evidence(analysis, "CE", "reversal", now)
    assert not entry_evidence(analysis, "CE", "momentum", now + timedelta(minutes=3))


def test_engulfing_rule_and_no_overnight_two_candle_setup():
    bars = parse_minutes(payload([[r[0], 100, 105, 95, 100] for r in rows("2026-08-26", 30)]), NOW)
    bars[-2].update(open=102, close=99)
    bars[-1].update(open=98, close=103)
    assert any(
        p["name"] == "engulfing" and p["direction"] == "bullish"
        for p in detect_patterns(bars, indicators(bars), None, None)
    )
    bars[-2]["at"] = "2026-08-25T15:30:00+05:30"
    assert not any(p["name"] == "engulfing" for p in detect_patterns(bars, indicators(bars), None, None))


@pytest.mark.parametrize(
    "name,direction,last,prev,regime",
    [
        ("breakout_retest", "bullish", (102, 105, 100.9, 104), (100, 104, 99, 103), "uptrend"),
        ("breakout_retest", "bearish", (98, 99.1, 95, 96), (100, 101, 96, 97), "downtrend"),
        ("ema_pullback", "bullish", (100, 104, 99, 103), None, "uptrend"),
        ("ema_pullback", "bearish", (100, 101, 96, 97), None, "downtrend"),
        ("hammer", "bullish", (100, 101.5, 97, 101), None, "range"),
        ("shooting_star", "bearish", (101, 104, 99.5, 100), None, "range"),
        ("range_rejection", "bullish", (99.5, 101, 98.9, 100.5), None, "range"),
        ("range_rejection", "bearish", (100.5, 101.1, 99, 99.5), None, "range"),
    ],
)
def test_explicit_setup_rules(name, direction, last, prev, regime):
    bars = parse_minutes(payload([[r[0], 100, 101, 99, 100] for r in rows("2026-08-26", 30)]), NOW)
    bars[-1].update(dict(zip(("open", "high", "low", "close"), last)))
    if prev:
        bars[-2].update(dict(zip(("open", "high", "low", "close"), prev)))
    metrics = {"regime": regime, "atr14": 2, "ema9": 100}
    assert any(p["name"] == name and p["direction"] == direction for p in detect_patterns(bars, metrics, None, None))


def test_market_fetches_full_context_and_reuses_daily_cache(monkeypatch):
    import io

    from kiwit.options_market import BankNiftyMarket

    class Broker:
        def __init__(self):
            self.requests = []

        def banknifty_candles(self, start, end):
            self.requests.append((start, end))
            data = (
                rows("2026-08-26", 45)
                if start.date() == NOW.date()
                else [row for d in HISTORY_DATES for row in rows(d)]
            )
            return payload(data)

        def quote(self, symbol, segment):
            # The trade happened during network I/O, after the original scan timestamp.
            return {
                "bid_price": 100,
                "offer_price": 101,
                "bid_quantity": 60,
                "offer_quantity": 60,
                "last_trade_time": int((NOW + timedelta(seconds=1)).timestamp() * 1000),
            }

    csv = "exchange,segment,underlying_symbol,instrument_type,expiry_date,buy_allowed,sell_allowed,is_reserved,lot_size,freeze_quantity,trading_symbol,strike_price,tick_size\nNSE,FNO,BANKNIFTY,CE,2026-09-29,1,1,0,30,601,BANKNIFTY26SEP55000CE,55000,.05\n"
    monkeypatch.setattr("kiwit.options_market.urllib.request.urlopen", lambda *a, **k: io.BytesIO(csv.encode()))
    broker = Broker()
    market = BankNiftyMarket(broker, clock=lambda: NOW + timedelta(seconds=2))
    first = market.snapshot(NOW)
    assert first["chart_analysis"]["ready"] and len(first["candidates"]) == 1
    assert len(broker.requests) == 2
    second = market.snapshot(NOW, cached_context=first["chart_cache"])
    assert len(broker.requests) == 3 and second["chart_analysis"] == first["chart_analysis"]
    refreshed = market.snapshot(NOW, cached_context=dict(first["chart_cache"], version="banknifty-chart-v1"))
    assert len(broker.requests) == 5
    assert refreshed["chart_analysis"]["previous_calendar_week"]["coverage"]["status"] == "complete"
    ai_snapshot = {k: v for k, v in first.items() if k not in ("chart_cache", "underlying_history")}
    ai_snapshot["chart_analysis"] = {k: v for k, v in first["chart_analysis"].items() if k != "chart_bars"}
    ai_snapshot["history"] = first["underlying_history"]
    ai_snapshot["candidates"] = first["candidates"] * 6
    ai_snapshot["previous_decisions"] = [{"summary": "x" * 600}] * 3
    assert len(request_body(ai_snapshot)) < 20000
    from kiwit.playbooks import select_plans

    a = ai_snapshot["chart_analysis"]
    a["timeframes"]["5m"].update(regime="uptrend", atr14=100)
    a["timeframes"]["15m"]["regime"] = "uptrend"
    a["patterns"] = [
        {
            "id": name + "_bullish",
            "name": name,
            "at": NOW.isoformat(),
            "strategy": "momentum",
            "direction": "bullish",
            "observed_close": float(first["spot"]),
            "invalidation": float(first["spot"]) - 50,
        }
        for name in ("opening_range_breakout", "breakout_retest", "ema_pullback")
    ]
    state = {"day": str(NOW.date()), "amount": "100000", "cash": "100000", "loss_pct": "5", "profit_pct": "10"}
    ai_snapshot["strategy_selection"] = select_plans(ai_snapshot, state, NOW + timedelta(seconds=2))
    assert len(ai_snapshot["strategy_selection"]["plans"]) == 3
    assert len(request_body(ai_snapshot)) < 20000


@pytest.mark.parametrize("close", [98, 102])
def test_range_rejection_requires_close_inside_both_boundaries(close):
    bars = parse_minutes(payload([[r[0], 100, 101, 99, 100] for r in rows("2026-08-26", 30)]), NOW)
    bars[-1].update(open=100, high=103, low=97, close=close)
    patterns = detect_patterns(bars, {"regime": "range", "atr14": 2, "ema9": 100}, None, None)
    assert not any(p["name"] == "range_rejection" for p in patterns)
