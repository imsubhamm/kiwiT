import json
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from kiwit.audit import HashChainAuditLog
from kiwit.marketdata.canonical import IST, Candle, Instrument, Session, TradingCalendar
from kiwit.marketdata.features import FEATURE_VERSION, FeatureEngine
from kiwit.marketdata.history import HistoricalReplay, HistoricalStore
from kiwit.marketdata.live import IngestResult

START = datetime(2026, 9, 10, 9, 15, tzinfo=IST)
INSTRUMENT = Instrument("BANKNIFTY", segment="INDEX", series="INDEX")
CALENDAR = TradingCalendar("test-v1", (Session(START.date()),))
MINUTE = timedelta(minutes=1)


def bars(count=40, *, volume=100, flat=False):
    result = []
    for i in range(count):
        price = 100 if flat else 100 + i
        result.append(
            Candle(
                INSTRUMENT,
                START + i * MINUTE,
                START + (i + 1) * MINUTE,
                price,
                price + 1,
                price - 1,
                price,
                volume,
                "fixture",
            )
        )
    return tuple(result)


def snapshot(data=None, count=40, **kwargs):
    return FeatureEngine(CALENDAR, **kwargs).compute(
        IngestResult(candles=data or bars(count)), INSTRUMENT, START + count * MINUTE
    )


def test_reference_values_for_linear_trend():
    result = snapshot()
    assert result.ready
    v = result.values
    assert v["ema_9"] == 135
    assert v["ema_21"] == 129
    assert v["ema_9_slope"] == pytest.approx(135 / 134 - 1)
    assert v["rsi_14"] == 100
    assert v["atr_14"] == 2
    assert v["adx_14"] == 100
    assert v["macd"] == 7
    assert v["macd_signal"] == 7
    assert v["macd_histogram"] == 0
    assert v["return_1"] == pytest.approx(139 / 138 - 1)
    assert v["momentum_5"] == pytest.approx(139 / 134 - 1)
    assert v["relative_volume_20"] == 1
    assert v["vwap_distance"] == pytest.approx(139 / 119.5 - 1)
    assert v["compression_20"] == 1
    assert v["breakout_high_20"] == 0
    assert v["minutes_since_open"] == 40
    assert v["session_fraction"] == pytest.approx(40 / 375)


def test_warmup_boundaries_are_explicit():
    early = snapshot(count=14)
    assert early.values["rsi_14"] is None
    assert early.unavailable["rsi_14"] == "INSUFFICIENT_HISTORY"
    assert not early.ready
    assert snapshot(count=15).values["rsi_14"] == 100
    assert snapshot(count=27).values["adx_14"] is None
    assert snapshot(count=28).values["adx_14"] == 100
    assert snapshot(count=33).values["macd_signal"] is None
    assert snapshot(count=34).ready


def test_flat_market_has_finite_neutral_values():
    v = snapshot(bars(flat=True)).values
    assert v["rsi_14"] == 50
    assert v["adx_14"] == 0
    assert v["realized_volatility_20"] == 0
    assert v["macd"] == 0
    assert v["vwap_distance"] == 0


def test_missing_volume_never_imputed_and_can_be_required():
    result = snapshot(bars(volume=None))
    assert result.ready
    assert result.values["vwap_distance"] is None
    assert result.unavailable["vwap_distance"] == "MISSING_VOLUME"
    assert result.values["relative_volume_20"] is None
    assert not snapshot(bars(volume=None), required=("vwap_distance",)).ready
    zero = snapshot(bars(volume=0))
    assert zero.unavailable["vwap_distance"] == "ZERO_VOLUME"


def test_future_data_and_partial_higher_timeframe_do_not_leak():
    prefix = snapshot(count=11)
    data = bars(20)
    changed = data[:11] + tuple(replace(c, high=10000, close=9999) for c in data[11:])
    assert snapshot(changed, count=11).to_json_dict() == prefix.to_json_dict()
    # Eleventh minute belongs to a forming five-minute bucket, so context remains at minute ten.
    ten = snapshot(count=10)
    assert prefix.values["range_5m"] == ten.values["range_5m"]
    assert prefix.values["return_5m"] == ten.values["return_5m"]
    assert snapshot(count=4).values["range_5m"] is None


def test_gaps_conflicts_wrong_instrument_and_invalid_upstream_block_features():
    data = bars()
    for invalid in (
        data[1:],
        data[:5] + data[6:],
        data + (replace(data[0], close=101),),
        (replace(data[0], instrument=Instrument("OTHER")),) + data[1:],
    ):
        result = snapshot(invalid)
        assert not result.ready
        assert result.reasons
        assert all(value is None for value in result.values.values())
    engine = FeatureEngine(CALENDAR)
    result = engine.compute(IngestResult(candles=data, reasons=("STALE_CANDLES",)), INSTRUMENT, START + 40 * MINUTE)
    assert result.reasons == ("STALE_CANDLES",)
    assert not result.ready


def test_live_and_replay_have_identical_features_and_input_hash(tmp_path):
    data = bars()
    store = HistoricalStore(tmp_path / "history.sqlite3")
    for candle in reversed(data):
        store.record([candle], available_at=candle.closed_at, raw_payload=b"fixture", source="fixture")
    now = START + 40 * MINUTE
    replay = HistoricalReplay(store, CALENDAR, clock=lambda: now)

    class LiveFixture:
        def banknifty_candles(self, start, end):
            return IngestResult(candles=data)

    engine = FeatureEngine(CALENDAR)
    assert engine.from_feed(LiveFixture(), now).to_json_dict() == engine.from_feed(replay, now).to_json_dict()


def test_model_boundary_logs_version_and_blocks_unready_inputs(tmp_path):
    audit = HashChainAuditLog(tmp_path / "audit.jsonl")
    engine = FeatureEngine(CALENDAR)
    called = []

    def predict(values):
        called.append(values)
        return {"action": "NO_TRADE", "score": 0.5}

    decision = engine.decide(snapshot(), model_id="test-model-v1", predict=predict, audit=audit)
    assert decision["score"] == 0.5
    assert len(called) == 1
    rejected = engine.decide(snapshot(count=1), model_id="test-model-v1", predict=predict, audit=audit)
    assert rejected["action"] == "NO_TRADE"
    assert len(called) == 1
    records = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert all(r["payload"]["features"]["feature_version"] == FEATURE_VERSION for r in records)
    assert all(r["payload"]["features"]["input_digest"] for r in records)
    assert audit.verify()


def test_numerical_overflow_fails_closed():
    data = tuple(replace(c, open="1e999", high="1e999", low="1e999", close="1e999") for c in bars())
    result = snapshot(data)
    assert result.reasons == ("INVALID_NUMERICAL_INPUT",)
    assert not result.ready


def test_empty_closed_sessions_and_immutable_snapshot():
    engine = FeatureEngine(CALENDAR)
    assert not engine.compute(IngestResult(), INSTRUMENT, START).ready
    assert not engine.compute(IngestResult(), INSTRUMENT, START + timedelta(days=1)).ready
    with pytest.raises(TypeError):
        snapshot().values["rsi_14"] = 10
    with pytest.raises(ValueError):
        FeatureEngine(CALENDAR, required=("not_a_feature",))


def test_rsi_reference_seed_and_audit_failure(tmp_path):
    prices = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89, 46.03, 45.61, 46.28, 46.28]
    data = tuple(replace(c, open=p, close=p, high=p + 1, low=p - 1) for c, p in zip(bars(15), prices))
    assert snapshot(data, count=15).values["rsi_14"] == pytest.approx(70.4641350211)
    # An unwritable audit destination cannot yield a model decision.
    audit = HashChainAuditLog(tmp_path / "directory")
    audit.path.mkdir()
    with pytest.raises(OSError):
        FeatureEngine(CALENDAR).decide(
            snapshot(), model_id="fixture", predict=lambda _: {"action": "NO_TRADE"}, audit=audit
        )
