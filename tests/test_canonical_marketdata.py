from dataclasses import replace
from datetime import datetime, timedelta
from itertools import permutations

import pytest

from kiwit.marketdata.canonical import (
    IST,
    Candle,
    Instrument,
    Quote,
    Session,
    TradingCalendar,
    archive_candle,
    assess_market,
    groww_candles,
    ordered_candles,
)
from kiwit.marketdata.models import NormalizedBar
from kiwit.marketdata.pipeline import MarketDataPipeline

START = datetime(2026, 9, 10, 9, 15, tzinfo=IST)
INSTRUMENT = Instrument("NIFTYBEES")
CALENDAR = TradingCalendar("test-v1", (Session(START.date()),))


def candle(offset=0):
    opened = START + timedelta(minutes=offset)
    return Candle(INSTRUMENT, opened, opened + timedelta(minutes=1), 100, 102, 99, 101, 10, "test")


def test_replay_roundtrip_and_permutation_invariance():
    bars = (candle(), candle(1), candle())
    for order in permutations(bars):
        assert ordered_candles(order) == (candle(), candle(1))
    assert Candle.from_json_dict(candle().to_json_dict()) == candle()
    for order in permutations((candle(), replace(candle(), close=100))):
        with pytest.raises(ValueError, match="Conflicting"):
            ordered_candles(order)
        assert assess_market(order, INSTRUMENT, START + timedelta(minutes=2), CALENDAR).invalid_market_state


@pytest.mark.parametrize(
    "change",
    [
        {"opened_at": START.replace(tzinfo=None)},
        {"open": "NaN"},
        {"high": 90},
        {"volume": -1},
        {"volume": 1.5},
        {"schema_version": "future"},
    ],
)
def test_invalid_contract_rejected(change):
    with pytest.raises(ValueError):
        replace(candle(), **change)


def test_market_gaps_staleness_missing_quotes_and_future_data():
    now = START + timedelta(minutes=3)
    assert assess_market([candle(i) for i in range(3)], INSTRUMENT, now, CALENDAR).trade_allowed
    assert "MISSING_CANDLES" in assess_market([], INSTRUMENT, now, CALENDAR).reasons
    assert "MISSING_OR_MISALIGNED_CANDLES" in assess_market([candle(1)], INSTRUMENT, now, CALENDAR).reasons
    assert "STALE_CANDLES" in assess_market([candle()], INSTRUMENT, now + timedelta(minutes=1), CALENDAR).reasons
    assert "MISSING_QUOTE" in assess_market([candle()], INSTRUMENT, now, CALENDAR, require_quote=True).reasons
    assert "FUTURE_OR_FORMING_CANDLE" in assess_market([candle(3)], INSTRUMENT, now, CALENDAR).reasons
    quote = Quote(INSTRUMENT, START, START, 100, "test")
    assert "STALE_QUOTE" in assess_market([candle()], INSTRUMENT, now, CALENDAR, quote=quote).reasons
    with pytest.raises(ValueError):
        replace(quote, exchange_at=now)


def test_calendar_unknown_special_sessions_and_close_boundary():
    assert assess_market([], INSTRUMENT, START + timedelta(days=1), CALENDAR).reasons == ("UNKNOWN_OR_CLOSED_SESSION",)
    assert assess_market([], INSTRUMENT, START.replace(hour=15, minute=30), CALENDAR).reasons == ("OUTSIDE_SESSION",)
    weekend = START.date() + timedelta(days=2)
    special = TradingCalendar("special", (Session(weekend),))
    assert special.session(weekend) is not None
    with pytest.raises(ValueError):
        TradingCalendar("bad", (Session(weekend), Session(weekend)))


def test_live_and_archive_share_contract_and_archive_publishes_it(tmp_path):
    row = [START.isoformat(), 100, 102, 99, 101, 10]
    payload = {"interval_in_minutes": 1, "candles": [row, row]}
    live = groww_candles(payload, START + timedelta(minutes=1), INSTRUMENT, CALENDAR)
    assert len(live) == 1
    assert live[0].closed_at == candle().closed_at
    assert groww_candles(payload, START, INSTRUMENT, CALENDAR) == ()
    archive = NormalizedBar(START.date(), "NIFTYBEES", "EQ", 100, 102, 99, 101, 10, "hash", "nse")
    assert archive_candle(archive).schema_version == live[0].schema_version
    path = tmp_path / "bars.csv"
    MarketDataPipeline._write_bars(path, [archive])
    import json

    stored = Candle.from_json_dict(json.loads(path.with_suffix(".market-v1.jsonl").read_text()))
    assert stored == archive_candle(archive)
