import json
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from kiwit.decision_journal import DecisionJournal, FeatureDecisionAudit
from kiwit.marketdata.canonical import IST, Candle, Instrument, Session, TradingCalendar
from kiwit.marketdata.features import FeatureEngine
from kiwit.marketdata.history import HistoricalStore
from kiwit.marketdata.live import IngestResult

START = datetime(2026, 9, 10, 9, 15, tzinfo=IST)
INSTRUMENT = Instrument("BANKNIFTY", segment="INDEX", series="INDEX")
CALENDAR = TradingCalendar("fixture", (Session(START.date()),))
MINUTE = timedelta(minutes=1)
AT = START + 40 * MINUTE


def candle(i):
    return Candle(
        INSTRUMENT, START + i * MINUTE, START + (i + 1) * MINUTE, 100 + i, 102 + i, 99 + i, 101 + i, None, "fixture"
    )


def setup(tmp_path):
    history = HistoricalStore(tmp_path / "history.sqlite3")
    market = tuple(candle(i) for i in range(40))
    for c in market:
        history.record(
            [c], available_at=c.closed_at, raw_payload=json.dumps(c.to_json_dict()).encode(), source="fixture"
        )
    features = FeatureEngine(CALENDAR).compute(IngestResult(candles=market), INSTRUMENT, AT)
    journal = DecisionJournal(tmp_path / "journal.sqlite3", history, secrets=("super-private-key",))
    return journal, history, market, features


def record(journal, market, features, decision_id="rejected", **kwargs):
    return journal.record(
        decision_id,
        features,
        market,
        action=kwargs.pop("action", "REJECT"),
        reason_codes=["LOW_CONFIDENCE"],
        models=[{"version": "model-v1", "scores": {"trend": 0.3}}],
        candidate={"side": "LONG"},
        **kwargs,
    )


def future(history, through=100, *, delay=None, omit=None):
    for i in range(40, through):
        if i == omit:
            continue
        c = candle(i)
        history.record(
            [c], available_at=delay or c.closed_at, raw_payload=json.dumps(c.to_json_dict()).encode(), source="fixture"
        )


def test_reconstruct_rejected_and_paper_execution_after_restart(tmp_path):
    journal, history, market, features = setup(tmp_path)
    record(journal, market, features)
    record(journal, market, features, "accepted", action="TRADE", risk_checks={"approved": True})
    journal.append_event(
        "accepted",
        "fill-1",
        at=AT + MINUTE,
        kind="execution",
        details={
            "mode": "PAPER",
            "status": "FILLED",
            "position_id": "paper-position",
            "fills": [
                {
                    "instrument": "BANKNIFTY",
                    "at": (AT + MINUTE).isoformat(),
                    "price": "140",
                    "quantity": 1,
                    "side": "BUY",
                    "fees": "0.2",
                }
            ],
        },
    )
    reopened = DecisionJournal(journal.path, history)
    rejected = reopened.read("rejected", as_of=AT)
    assert rejected["decision"]["action"] == "REJECT"
    assert rejected["decision"]["features"]["input_digest"] == features.input_digest
    assert len(rejected["decision"]["market"]) == 40
    assert rejected["decision"]["raw_snapshot_references"]
    assert reopened.read("accepted", as_of=AT)["events"] == []
    assert reopened.read("accepted", as_of=AT + MINUTE)["events"][0]["details"]["fills"][0]["price"] == "140"
    assert len(reopened.export(as_of=AT + MINUTE)) == 2


def test_credentials_redacted_in_structures_and_prompt_text(tmp_path):
    journal, _, market, features = setup(tmp_path)
    record(
        journal,
        market,
        features,
        llm={
            "prompt_version": "v1",
            "model_version": "llm-v1",
            "prompt": "context super-private-key Bearer abc.def secret=hidden",
            "output": {"nested": {"api_key": "private"}, "text": "super-private-key"},
        },
        risk_checks={"authorization": "Bearer private", "ok": True},
    )
    raw = journal.path.read_bytes()
    for secret in (b"super-private-key", b"abc.def", b"hidden", b'"private"'):
        assert secret not in raw
    assert "[REDACTED]" in json.dumps(journal.read("rejected", as_of=AT))


def test_retries_are_idempotent_and_conflicting_evidence_rejected(tmp_path):
    journal, _, market, features = setup(tmp_path)
    record(journal, market, features)
    record(journal, market, features)
    with pytest.raises(ValueError, match="different evidence"):
        record(journal, market, features, action="NO_TRADE")
    for _ in range(2):
        journal.append_event("rejected", "risk", at=AT, kind="risk", details={"approved": False})
    assert len(journal.read("rejected", as_of=AT)["events"]) == 1
    with pytest.raises(ValueError):
        journal.append_event("rejected", "risk", at=AT, kind="risk", details={"approved": True})


def test_future_and_unbacked_feature_snapshots_rejected(tmp_path):
    journal, _, market, features = setup(tmp_path)
    with pytest.raises(ValueError, match="digest"):
        record(journal, market[:-1], features)
    with pytest.raises(ValueError, match="future"):
        record(journal, market + (candle(40),), features)
    with pytest.raises(ValueError, match="differs"):
        altered = market[:-1] + (replace(market[-1], close=140.5),)
        computed = FeatureEngine(CALENDAR).compute(IngestResult(candles=altered), INSTRUMENT, AT)
        record(journal, altered, computed)
    with pytest.raises(ValueError):
        record(journal, market, replace(features, input_digest="wrong"))


def test_outcomes_for_rejected_candidates_are_delayed_and_separate(tmp_path):
    journal, history, market, features = setup(tmp_path)
    record(journal, market, features)
    future(history)
    assert journal.label_outcomes("rejected", as_of=AT + 4 * MINUTE, calendar=CALENDAR)[5] == "PENDING"
    result = journal.label_outcomes("rejected", as_of=AT + 60 * MINUTE, calendar=CALENDAR)
    assert result == {5: "LABELED", 15: "LABELED", 30: "LABELED", 60: "LABELED"}
    assert journal.read("rejected", as_of=AT)["outcomes"] == []
    labels = journal.read("rejected", as_of=AT + 60 * MINUTE)["outcomes"]
    assert len(labels) == 4
    assert float(labels[0]["forward_return"]) == pytest.approx(145 / 140 - 1)
    assert labels[0]["movement"] == "UP"
    assert journal.label_outcomes("rejected", as_of=AT + 61 * MINUTE, calendar=CALENDAR) == result
    assert journal.read("rejected", as_of=AT + 61 * MINUTE)["outcomes"] == labels


def test_gap_or_late_arrival_does_not_invent_outcome(tmp_path):
    journal, history, market, features = setup(tmp_path)
    record(journal, market, features)
    future(history, through=45, delay=AT + 7 * MINUTE)
    assert journal.label_outcomes("rejected", as_of=AT + 5 * MINUTE, calendar=CALENDAR)[5] == "MISSING_OUTCOME_DATA"
    assert journal.label_outcomes("rejected", as_of=AT + 7 * MINUTE, calendar=CALENDAR)[5] == "LABELED"
    future(history, through=55, delay=AT + 15 * MINUTE, omit=50)
    assert journal.label_outcomes("rejected", as_of=AT + 15 * MINUTE, calendar=CALENDAR)[15] == "MISSING_OUTCOME_DATA"


def test_journal_adapter_captures_real_feature_engine_decision(tmp_path):
    journal, _, market, features = setup(tmp_path)
    adapter = FeatureDecisionAudit(journal, "engine", features, market)
    result = FeatureEngine(CALENDAR).decide(
        features,
        model_id="specialist-v1",
        predict=lambda _: {"action": "NO_TRADE", "reason_codes": ["WEAK_SIGNAL"], "scores": {"trend": 0.2}},
        audit=adapter,
    )
    saved = journal.read("engine", as_of=AT)["decision"]
    assert saved["action"] == result["action"]
    assert saved["models"][0]["scores"] == {"trend": 0.2}
    assert saved["features"]["feature_version"] == features.version


def test_tampered_evidence_and_early_read_fail(tmp_path):
    journal, _, market, features = setup(tmp_path)
    record(journal, market, features)
    with pytest.raises(KeyError):
        journal.read("rejected", as_of=AT - MINUTE)
    with sqlite3.connect(journal.path) as db:
        db.execute("UPDATE decisions SET payload='{}'")
    with pytest.raises(ValueError, match="checksum"):
        journal.read("rejected", as_of=AT)


def test_execution_rejects_live_mode_and_incomplete_fill_details(tmp_path):
    journal, _, market, features = setup(tmp_path)
    record(journal, market, features)
    with pytest.raises(ValueError, match="paper"):
        journal.append_event("rejected", "live", at=AT, kind="execution", details={"mode": "LIVE"})
    with pytest.raises(ValueError, match="Fill evidence"):
        journal.append_event(
            "rejected",
            "missing",
            at=AT,
            kind="execution",
            details={"mode": "PAPER", "status": "FILLED", "position_id": "test", "fills": [{}]},
        )


def test_warmup_rejection_is_audited_without_model_call(tmp_path):
    journal, _, market, _ = setup(tmp_path)
    short = market[:1]
    engine = FeatureEngine(CALENDAR)
    features = engine.compute(IngestResult(candles=short), INSTRUMENT, START + MINUTE)
    adapter = FeatureDecisionAudit(journal, "warmup", features, short)

    def predict(_):
        raise AssertionError("Model must not run during warmup")

    engine.decide(features, model_id="fixture-v1", predict=predict, audit=adapter)
    saved = journal.read("warmup", as_of=AT)["decision"]
    assert saved["action"] == "NO_TRADE"
    assert saved["reason_codes"] == ["FEATURES_UNAVAILABLE"]


def test_horizons_never_cross_session_end(tmp_path):
    journal, history, market, features = setup(tmp_path)
    record(journal, market, features)
    future(history)
    shortened = TradingCalendar("short", (Session(START.date(), closes=(AT + 10 * MINUTE).astimezone(IST).time()),))
    results = journal.label_outcomes("rejected", as_of=AT + 60 * MINUTE, calendar=shortened)
    assert results[5] == "LABELED"
    assert results[15] == "OUTSIDE_SESSION"
