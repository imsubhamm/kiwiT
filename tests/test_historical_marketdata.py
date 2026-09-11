import json
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from kiwit.marketdata.canonical import IST, Candle, Instrument, Session, TradingCalendar
from kiwit.marketdata.history import HistoricalReplay, HistoricalStore, history_quality, replay_sessions
from kiwit.marketdata.live import GrowwMarketDataService, MarketDataUnavailable, ObservationStore

START = datetime(2026, 9, 10, 9, 15, tzinfo=IST)
INSTRUMENT = Instrument("BANKNIFTY", segment="INDEX", series="INDEX")
CALENDAR = TradingCalendar("fixture-v1", (Session(START.date(), closes=time(9, 18)),))
MINUTE = timedelta(minutes=1)


def bar(i=0, *, start=START, instrument=INSTRUMENT, interval=MINUTE):
    opened = start + i * interval
    return Candle(instrument, opened, opened + interval, 100, 102, 99, 101, None, "test")


def record(store, candle, at=None):
    return store.record(
        [candle],
        available_at=at or candle.closed_at,
        raw_payload=json.dumps(candle.to_json_dict()).encode(),
        source="test",
    )


def test_replay_orders_sessions_and_never_exposes_late_or_future_data(tmp_path):
    store = HistoricalStore(tmp_path / "history.sqlite3")
    record(store, bar(2))  # Store intentionally in reverse order.
    record(store, bar(1))
    record(store, bar(0), START + 2 * MINUTE)  # Late arrival must not appear at first tick.
    frames = list(replay_sessions(store, CALENDAR, INSTRUMENT, START, START + 3 * MINUTE))
    assert [f.as_of for f in frames] == [(START + i * MINUTE).astimezone(UTC) for i in (1, 2, 3)]
    assert frames[0].result.candles == ()
    assert frames[0].quality.missing_opens == (START.astimezone(UTC),)
    assert frames[1].result.require_valid().candles == (bar(), bar(1))
    assert frames[2].result.require_valid().candles == (bar(), bar(1), bar(2))
    for frame in frames:
        assert all(c.closed_at <= frame.as_of for c in frame.result.candles)
    # Reopening the database reproduces exact frames, including invalid ones.
    reopened = HistoricalStore(store.path)
    assert frames == list(replay_sessions(reopened, CALENDAR, INSTRUMENT, START, START + 3 * MINUTE))


def test_clock_bound_feed_clamps_future_query_and_matches_live_result(tmp_path):
    store = HistoricalStore(tmp_path / "history.sqlite3")
    for i in range(3):
        record(store, bar(i))
    feed = HistoricalReplay(store, CALENDAR, clock=lambda: START + MINUTE)
    result = feed.banknifty_candles(START, START + 3 * MINUTE)
    assert result.require_valid().candles == (bar(),)
    assert feed.snapshot(INSTRUMENT, START, START + 3 * MINUTE).quality.missing_opens == ()


def test_head_middle_tail_and_whole_session_gaps(tmp_path):
    for candles, missing in [([bar(1)], (0, 2)), ([bar(), bar(2)], (1,)), ([], (0, 1, 2))]:
        report = history_quality(candles, CALENDAR, START, START + 3 * MINUTE, as_of=START + 3 * MINUTE)
        assert report.missing_opens == tuple((START + i * MINUTE).astimezone(UTC) for i in missing)
        assert not report.valid
    empty = HistoricalStore(tmp_path / "empty.sqlite3")
    frames = list(replay_sessions(empty, CALENDAR, INSTRUMENT, START, START + 3 * MINUTE))
    assert len(frames) == 3
    with pytest.raises(MarketDataUnavailable):
        frames[-1].result.require_valid()


def test_unknown_calendar_and_misaligned_data(tmp_path):
    report = history_quality(
        [bar(start=START + timedelta(seconds=1))], CALENDAR, START, START + 3 * MINUTE, as_of=START + 3 * MINUTE
    )
    assert "UNEXPECTED_CANDLES" in report.reasons
    unknown = history_quality(
        [], CALENDAR, START + timedelta(days=1), START + timedelta(days=2), as_of=START + timedelta(days=2)
    )
    assert unknown.reasons == ("NO_DECLARED_SESSIONS",)
    mismatch = history_quality([], CALENDAR, START, START + 3 * MINUTE, as_of=START + 3 * MINUTE, interval=2 * MINUTE)
    assert "SESSION_TIMEFRAME_MISMATCH" in mismatch.reasons


def test_index_isolates_instrument_date_and_timeframe(tmp_path):
    store = HistoricalStore(tmp_path / "history.sqlite3")
    record(store, bar())
    record(store, bar(instrument=Instrument("OTHER")))
    record(store, bar(interval=2 * MINUTE))
    record(store, bar(start=START + timedelta(days=1)))
    assert store.candles(INSTRUMENT, START, START + 3 * MINUTE, as_of=START + timedelta(days=2)) == (bar(),)
    assert store.candles(
        INSTRUMENT, START, START + 3 * MINUTE, as_of=START + timedelta(days=2), interval=2 * MINUTE
    ) == (bar(interval=2 * MINUTE),)
    with sqlite3.connect(store.path) as db:
        indexes = {r[1] for r in db.execute("PRAGMA index_list(historical_candles)")}
    assert {"history_as_of", "history_session_timeframe"} <= indexes


def test_conflict_rolls_back_entire_raw_and_normalized_batch(tmp_path):
    store = HistoricalStore(tmp_path / "history.sqlite3")
    record(store, bar(1))
    with pytest.raises(ValueError, match="Conflicting"):
        store.record(
            [bar(), replace(bar(1), close=100)], available_at=START + 3 * MINUTE, raw_payload=b"conflict", source="test"
        )
    assert store.candles(INSTRUMENT, START, START + 3 * MINUTE, as_of=START + 3 * MINUTE) == (bar(1),)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM historical_raw").fetchone()[0] == 1


def test_idempotency_raw_retention_and_no_backdating(tmp_path):
    store = HistoricalStore(tmp_path / "history.sqlite3")
    assert record(store, bar(), START + 2 * MINUTE) == 1
    assert record(store, bar(), START + 3 * MINUTE) == 0
    with pytest.raises(ValueError, match="backdate"):
        record(store, bar())
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT body FROM historical_raw").fetchone()[0] == json.dumps(bar().to_json_dict()).encode()
        assert db.execute("SELECT count(*) FROM historical_provenance").fetchone()[0] == 1
    assert store.candles(INSTRUMENT, START, START + MINUTE, as_of=START + MINUTE) == ()


@pytest.mark.parametrize("at", [START, START.replace(tzinfo=None)])
def test_unknown_or_premature_availability_rejected(tmp_path, at):
    store = HistoricalStore(tmp_path / "history.sqlite3")
    with pytest.raises(ValueError):
        record(store, bar(), at)


def test_special_session_and_holidays_no_synthetic_weekdays(tmp_path):
    weekend = START + timedelta(days=2)
    calendar = TradingCalendar("special", (Session(weekend.date(), closes=time(9, 16)), CALENDAR.sessions[0]))
    store = HistoricalStore(tmp_path / "history.sqlite3")
    record(store, bar(start=weekend))
    frames = list(replay_sessions(store, calendar, INSTRUMENT, START, weekend + MINUTE))
    assert len(frames) == 4  # Three Thursday ticks, no Friday, one declared Saturday tick.
    assert frames[-1].result.require_valid().candles == (bar(start=weekend),)


def test_live_ingestion_records_real_receipt_availability(tmp_path):
    class Client:
        def banknifty_candles(self, start, end):
            return {
                "interval_in_minutes": 1,
                "candles": [[START.isoformat(), 100, 102, 99, 101, None]],
                "ignored_extra": "not persisted",
            }

    store = HistoricalStore(tmp_path / "history.sqlite3")
    live = GrowwMarketDataService(
        Client(),
        ObservationStore(tmp_path / "observations.sqlite3"),
        CALENDAR,
        clock=lambda: START + 2 * MINUTE,
        history=store,
    )
    result = live.banknifty_candles(START, START + 2 * MINUTE)
    assert len(result.candles) == 1
    assert store.candles(INSTRUMENT, START, START + MINUTE, as_of=START + MINUTE) == ()
    assert len(store.candles(INSTRUMENT, START, START + 2 * MINUTE, as_of=START + 2 * MINUTE)) == 1
    with sqlite3.connect(store.path) as db:
        raw = json.loads(db.execute("SELECT body FROM historical_raw").fetchone()[0])
    assert set(raw) == {"interval_in_minutes", "candles"}


def test_cli_import_and_replay_roundtrip(tmp_path):
    source = tmp_path / "candles.jsonl"
    source.write_text(json.dumps(bar().to_json_dict()) + "\n")
    calendar = tmp_path / "calendar.json"
    calendar.write_text(json.dumps({"version": "test", "sessions": [{"day": str(START.date()), "closes": "09:16"}]}))
    script = Path(__file__).resolve().parents[1] / "scripts/replay_market_data.py"
    base = [sys.executable, str(script), "--db", str(tmp_path / "history.sqlite3")]
    imported = subprocess.run(
        base + ["import", "--file", str(source), "--available-at", (START + MINUTE).isoformat(), "--source", "fixture"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(imported.stdout) == {"inserted": 1}
    replayed = subprocess.run(
        base
        + [
            "replay",
            "--calendar",
            str(calendar),
            "--start",
            START.isoformat(),
            "--end",
            (START + MINUTE).isoformat(),
            "--symbol",
            "BANKNIFTY",
            "--segment",
            "INDEX",
            "--series",
            "INDEX",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    frame = json.loads(replayed.stdout)
    assert not frame["invalid_market_state"]
    assert Candle.from_json_dict(frame["candles"][0]) == bar()


def test_future_values_cannot_change_earlier_training_frames(tmp_path):
    prefix = []
    for label, future_close in [("baseline", 101), ("changed", 100)]:
        store = HistoricalStore(tmp_path / f"{label}.sqlite3")
        record(store, bar())
        record(store, replace(bar(1), close=future_close))
        prefix.append(list(replay_sessions(store, CALENDAR, INSTRUMENT, START, START + MINUTE)))
    assert prefix[0] == prefix[1]


def test_replay_rejects_unknown_sessions_and_unsupported_timeframes(tmp_path):
    store = HistoricalStore(tmp_path / "history.sqlite3")
    with pytest.raises(ValueError, match="No declared sessions"):
        list(replay_sessions(store, CALENDAR, INSTRUMENT, START + timedelta(days=1), START + timedelta(days=2)))
    with pytest.raises(ValueError, match="divide"):
        list(replay_sessions(store, CALENDAR, INSTRUMENT, START, START + 3 * MINUTE, interval=2 * MINUTE))
    with pytest.raises(ValueError, match="positive"):
        store.candles(INSTRUMENT, START, START + MINUTE, as_of=START, interval=timedelta(0))
