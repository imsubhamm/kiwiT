import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import pytest

from kiwit.banknifty import BankNiftyService
from kiwit.chart_analysis import VERSION
from kiwit.options_ai import parse_response, request_body
from kiwit.options_market import completed_candles, contracts_from_csv, executable_quote
from kiwit.options_risk import quantity_for
from kiwit.paper_session import BoundDatabase

NOW = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)
CONTRACT = {
    "symbol": "BANKNIFTY26SEP55000CE",
    "expiry": "2026-09-29",
    "kind": "CE",
    "strike": "55000",
    "lot": 30,
    "tick": "0.05",
    "freeze": 601,
}


def quote(now=NOW, bid="100", ask="101", size=600):
    return {"stamp": now.isoformat(), "bid": bid, "ask": ask, "bid_size": size, "ask_size": size}


def test_quotes_require_timestamp_and_executable_depth():
    payload = {
        "bid_price": 100,
        "offer_price": 101,
        "bid_quantity": 60,
        "offer_quantity": 60,
        "last_trade_time": int(NOW.timestamp() * 1000),
    }
    assert executable_quote(payload, NOW)["bid"] == "100"
    for changes in (
        {"last_trade_time": None},
        {"bid_quantity": 0},
        {"offer_price": 200},
        {"offer_price": 99},
        {"last_trade_time": int((NOW - timedelta(minutes=2)).timestamp() * 1000)},
        {"last_trade_time": int((NOW + timedelta(seconds=1)).timestamp() * 1000)},
    ):
        with pytest.raises((ValueError, ArithmeticError)):
            executable_quote(dict(payload, **changes), NOW)


def test_contracts_come_from_master_no_invented_lot_or_expiry():
    header = "exchange,segment,underlying_symbol,instrument_type,expiry_date,buy_allowed,sell_allowed,is_reserved,lot_size,freeze_quantity,trading_symbol,strike_price,tick_size\n"
    row = "NSE,FNO,BANKNIFTY,CE,2026-09-29,1,1,0,30,601,BANKNIFTY26SEP55000CE,55000,.05\n"
    assert contracts_from_csv(header + row, NOW.date()) == [CONTRACT]
    for bad in (
        row.replace("BANKNIFTY,", "NIFTY,"),
        row.replace("2026-09-29", "2026-08-26"),
        row.replace(",1,1,0,", ",1,1,1,"),
        row.replace(",30,", ",0,"),
    ):
        with pytest.raises(ValueError):
            contracts_from_csv(header + bad, NOW.date())


def test_sizing_whole_lots_and_never_forces_trade():
    state = {"amount": "100000", "cash": "100000", "loss_pct": "5"}
    qty = quantity_for(state, CONTRACT, quote())
    assert qty > 0 and qty % 30 == 0 and qty < 601
    assert quantity_for(dict(state, amount="1000", cash="1000"), CONTRACT, quote()) == 0
    assert quantity_for(state, CONTRACT, quote(size=29)) == 0


def response(decision=None, status="completed"):
    return {
        "status": status,
        "output": [
            {
                "type": "message",
                "content": [
                    {
                        "type": "output_text",
                        "text": json.dumps(
                            decision
                            or {
                                "action": "HOLD",
                                "symbol": "",
                                "strategy": "no_trade",
                                "plan_id": "",
                                "summary": "Wait",
                            }
                        ),
                    }
                ],
            }
        ],
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


def test_ai_schema_and_budget_bounds():
    decision, usage = parse_response(response())
    assert decision["action"] == "HOLD"
    assert D(usage["budget_charge_usd"]) == D(".002")
    with pytest.raises(ValueError):
        parse_response(response(status="incomplete"))
    with pytest.raises(ValueError):
        parse_response(response({"action": "SHORT", "symbol": "X", "strategy": "momentum", "summary": ""}))
    with pytest.raises(ValueError):
        request_body({"huge": "x" * 21000})
    body = json.loads(request_body({"spot": "55000"}))
    assert body["store"] is False and body["max_output_tokens"] == 1000 and "tools" not in body


@pytest.mark.parametrize(
    "change",
    [
        {"action": "BUY"},
        {"plan_id": "invented"},
        {"symbol": "unexpected"},
        {"strategy": "momentum"},
        {"action": "EXIT"},
    ],
)
def test_ai_contract_rejects_inconsistent_action_and_plan(change):
    d = {"action": "HOLD", "symbol": "", "plan_id": "", "strategy": "no_trade", "summary": "Wait"}
    d.update(change)
    with pytest.raises(ValueError):
        parse_response(response(d))


def test_candle_fallback_excludes_forming_bars_and_stale_prices():
    rows = [[(NOW - timedelta(minutes=i)).isoformat(), 100, 102, 99, 101, None] for i in range(6)]
    payload = {"interval_in_minutes": 1, "candles": rows}
    result = completed_candles(payload, NOW)
    assert len(result) == 5  # row at NOW is still forming
    assert result[-1]["at"] == NOW.isoformat()
    with pytest.raises(ValueError, match="stale"):
        completed_candles(payload, NOW + timedelta(minutes=5))
    with pytest.raises(ValueError):
        completed_candles(dict(payload, interval_in_minutes=5), NOW)


def test_stop_during_ai_inference_prevents_entry(desk):
    service, _market, analyst, _clock = desk
    original = analyst.decide

    def stop_then_decide(snapshot):
        service.stop("test-concurrent-stop")
        return original(snapshot)

    analyst.decide = stop_then_decide
    assert warm(desk)["position"] is None
    assert service.status()["session"]["state"] == "completed"


def test_flat_zero_entry_session_can_resume_once_with_original_limits(desk):
    service, _market, _analyst, _clock = desk
    original = service.start(100000, 5, 10, "test")
    service.stop("test")
    resumed = service.start(100000, 5, 10, "test-resume")
    assert resumed["state"] == "running"
    assert resumed["resumes"] == 1
    service.stop("test")
    assert service.start(100000, 5, 10, "test")["state"] == "completed"
    with pytest.raises(ValueError, match="immutable"):
        service.start(100001, 5, 10, "test")
    events = service.status()["events"]
    assert any(e["kind"] == "run_resumed" and e["detail"]["limits_preserved"] for e in events)
    assert original["day"] == resumed["day"]


def test_hallucinated_contract_is_blocked(desk):
    service, _market, analyst, _clock = desk
    original = analyst.decide

    def wrong_contract(snapshot):
        decision, usage = original(snapshot)
        decision["symbol"] = "NIFTY-FAKE"
        return decision, usage

    analyst.decide = wrong_contract
    assert warm(desk)["position"] is None
    assert "outside" in service.status()["session"]["detail"]


def test_buy_without_matching_chart_evidence_is_blocked(desk):
    service, market, _analyst, _clock = desk
    original = market.snapshot

    def missing_evidence(now):
        snapshot = original(now)
        snapshot["chart_analysis"]["patterns"] = []
        return snapshot

    market.snapshot = missing_evidence
    assert warm(desk)["position"] is None
    assert "No eligible entry plan" in service.status()["session"]["detail"]


def test_incomplete_chart_context_blocks_ai_but_is_persisted(desk):
    service, market, analyst, _clock = desk
    original = market.snapshot

    def incomplete(now):
        snapshot = original(now)
        snapshot["chart_analysis"].update(ready=False, issues=["Missing history"])
        return snapshot

    market.snapshot = incomplete
    assert warm(desk)["position"] is None
    assert analyst.calls == 0
    assert service.status()["session"]["chart_analysis"]["issues"] == ["Missing history"]


@pytest.fixture
def db():
    url = os.getenv("KIWIT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set KIWIT_TEST_DATABASE_URL for isolated PostgreSQL tests")
    import psycopg
    from psycopg import sql

    with psycopg.connect(url) as conn, conn.transaction(force_rollback=True):
        schema = "kiwit_bn_test_" + uuid4().hex
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(sql.Identifier(schema)))
        for path in sorted(Path("migrations").glob("*.sql")):
            conn.execute(path.read_text().strip()[6:-7])
        yield BoundDatabase(conn)


class Market:
    def __init__(self):
        self.bid = "100"
        self.size = 600
        self.fail = False

    def quote(self, symbol, now, entry=True):
        if self.fail:
            raise ValueError("stale")
        return quote(now, bid=self.bid, size=self.size)

    def latest_underlying(self, now):
        if self.fail:
            raise ValueError("stale")
        return {"at": now.isoformat(), "spot": "55000"}

    def snapshot(self, now):
        if self.fail:
            raise ValueError("stale")
        return {
            "at": now.isoformat(),
            "spot_at": now.isoformat(),
            "spot": "55000",
            "candidates": [dict(CONTRACT, quote=self.quote(CONTRACT["symbol"], now))],
            "chart_analysis": {
                "version": VERSION,
                "at": now.isoformat(),
                "ready": True,
                "issues": [],
                "timeframes": {"5m": {"regime": "uptrend", "atr14": 100}, "15m": {"regime": "uptrend"}},
                "previous_calendar_week": {"coverage": {"status": "complete"}, "trend": "upward_bias"},
                "patterns": [
                    {
                        "id": "opening_range_breakout_bullish",
                        "name": "opening_range_breakout",
                        "direction": "bullish",
                        "strategy": "momentum",
                        "at": now.isoformat(),
                        "observed_close": 55000,
                        "invalidation": 54950,
                    }
                ],
            },
        }


class Analyst:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.action = "BUY"
        self.last_snapshot = None

    def decide(self, snapshot):
        self.calls += 1
        self.last_snapshot = snapshot
        if self.fail:
            raise TimeoutError()
        return {
            "action": self.action,
            "symbol": CONTRACT["symbol"],
            "strategy": "momentum" if self.action == "BUY" else "no_trade",
            "plan_id": snapshot["strategy_selection"]["plans"][0]["id"] if self.action == "BUY" else "",
            "summary": "Fixture only",
        }, {"input_tokens": 100, "output_tokens": 50, "budget_charge_usd": ".002"}


class Mailer:
    def __init__(self):
        self.reports = []

    def send_daily_report(self, report, dashboard_url):
        self.reports.append((report, dashboard_url))
        return "sent", ""


@pytest.fixture
def desk(db, monkeypatch):
    monkeypatch.setenv("KIWIT_BANKNIFTY_AI_ENABLED", "true")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-real-key")
    market, analyst, clock = Market(), Analyst(), [NOW]
    service = BankNiftyService(db, None, market=market, analyst=analyst, clock=lambda: clock[0])
    return service, market, analyst, clock


def warm(desk):
    service, _market, _analyst, clock = desk
    service.start(100000, 5, 10, "test")
    for i in range(5):
        clock[0] = NOW + timedelta(minutes=i)
        service.run_once()
    return service.status()["session"]


def test_run_auto_entry_exit_idempotence_and_restart(desk):
    service, market, analyst, clock = desk
    state = warm(desk)
    assert state["entries"] == 1 and state["position"]["quantity"] % 30 == 0
    service.run_once()
    assert analyst.calls == 1 and service.status()["session"]["entries"] == 1
    with pytest.raises(ValueError, match="immutable"):
        service.start(200000, 5, 10, "test")
    # New service object proves state is not in-memory only.
    restarted = BankNiftyService(service.store.database, None, market=market, analyst=analyst, clock=lambda: clock[0])
    restarted.stop("test")
    clock[0] += timedelta(minutes=1)
    restarted.run_once()
    assert restarted.status()["session"]["state"] == "completed"
    assert restarted.status()["session"]["position"] is None


def test_stale_feed_and_ai_timeout_do_not_buy(desk):
    service, market, analyst, clock = desk
    analyst.fail = True
    assert warm(desk)["position"] is None
    assert service.status()["budget"]["used_or_reserved_usd"] == ".20" or D(
        service.status()["budget"]["used_or_reserved_usd"]
    ) == D(".20")
    service.run_once()
    assert analyst.calls == 1
    market.fail = True
    clock[0] += timedelta(minutes=5)
    service.run_once()
    assert analyst.calls == 1


def test_partial_stop_exit_and_no_same_quote_double_fill(desk):
    service, market, _analyst, clock = desk
    state = warm(desk)
    original = state["position"]["quantity"]
    assert original > 30
    market.size = 30
    service.stop("test")
    clock[0] += timedelta(minutes=1)
    service.run_once()
    assert service.status()["session"]["position"]["quantity"] == original - 30
    service.run_once()
    assert service.status()["session"]["position"]["quantity"] == original - 30
    market.fail = True
    clock[0] += timedelta(minutes=1)
    service.run_once()
    assert service.status()["session"]["state"] == "stopping"


def test_stop_loss_exits_even_when_ai_fails(desk):
    service, market, analyst, clock = desk
    warm(desk)
    analyst.fail = True
    market.bid = "90"
    clock[0] += timedelta(minutes=1)
    service.run_once()
    assert service.status()["session"]["position"] is None
    assert any(e["kind"] == "paper_exit" for e in service.status()["events"])


def test_eod_no_postmarket_fill(desk):
    service, _market, _analyst, clock = desk
    warm(desk)
    clock[0] = NOW.replace(hour=10, minute=1)  # 15:31 IST
    service.run_once()
    assert service.status()["session"]["state"] == "stopping"
    assert service.status()["session"]["position"] is not None


def test_330_report_is_durable_emailed_once_and_honest_about_unresolved_position(desk):
    service, _market, _analyst, clock = desk
    mailer = Mailer()
    service.mailer = mailer
    warm(desk)
    clock[0] = NOW.replace(hour=10, minute=0)  # 15:30 IST
    service.run_once()
    report = service.status()["daily_reports"][0]
    assert report["cutoff"] == "15:30 Asia/Kolkata"
    assert report["execution"] == "paper-only"
    assert report["reconciled_flat"] is False
    assert report["open_position"]["symbol"] == CONTRACT["symbol"]
    assert report["delivery"]["status"] == "sent"
    assert report["delivery"]["attempts"] == 1
    assert len(mailer.reports) == 1
    clock[0] += timedelta(minutes=1)
    service.run_once()
    assert len(mailer.reports) == 1


def test_330_report_for_completed_flat_session(desk):
    service, _market, _analyst, clock = desk
    service.mailer = Mailer()
    warm(desk)
    service.stop("test")
    clock[0] += timedelta(minutes=1)
    service.run_once()
    assert service.status()["session"]["state"] == "completed"
    clock[0] = NOW.replace(hour=10, minute=0)
    service.run_once()
    report = service.status()["daily_reports"][0]
    assert report["reconciled_flat"] is True
    assert report["session_state"] == "completed"
    assert report["open_position"] is None


def test_budget_limits_reserve_before_network(desk):
    service, _market, analyst, _clock = desk
    service.start(100000, 5, 10, "test")
    with service.store.locked() as connection:
        connection.execute(
            "INSERT INTO banknifty_ai_calls(call_id,trading_date,slot,state,reserved_usd,snapshot) "
            "VALUES(%s,%s,0,'failed',18,'{}')",
            (uuid4(), NOW.date()),
        )
    warm(desk)
    assert analyst.calls == 0
    assert service.status()["session"]["position"] is None


@pytest.mark.parametrize("failure", ["unknown_plan", "price_moved", "underlying_reversed", "plan_expired"])
def test_ai_buy_is_revalidated_after_inference_and_rejection_is_audited(desk, failure):
    service, market, analyst, clock = desk
    original = analyst.decide

    def changed(snapshot):
        result, usage = original(snapshot)
        if failure == "unknown_plan":
            result["plan_id"] = "invented"
        elif failure == "price_moved":
            market.quote = lambda symbol, now, entry=True: quote(now, bid="103", ask="104")
        elif failure == "underlying_reversed":
            market.latest_underlying = lambda now: {"at": now.isoformat(), "spot": "54900"}
        else:
            clock[0] += timedelta(seconds=91)
        return result, usage

    analyst.decide = changed
    assert warm(desk)["position"] is None
    status = service.status()
    assert status["decisions"][0]["state"] == "rejected"
    assert status["decisions"][0]["result"]["validation_error"]
    assert any(e["kind"] == "blocked" and e["detail"].get("call_id") for e in status["events"])


@pytest.mark.parametrize("reason", ["underlying_invalidation", "time_exit"])
def test_playbook_exits_without_ai_and_keeps_plan_attribution(desk, reason):
    service, market, analyst, clock = desk
    state = warm(desk)
    assert state["position"]["entry_plan"]["playbook_id"] == "opening_range_breakout_v1"
    analyst.fail = True
    if reason == "underlying_invalidation":
        clock[0] += timedelta(minutes=1)
        market.latest_underlying = lambda now: {"at": now.isoformat(), "spot": "54900"}
    else:
        clock[0] += timedelta(minutes=46)
    service.run_once()
    status = service.status()
    assert status["session"]["position"] is None
    event = next(e for e in status["events"] if e["kind"] == "paper_exit")
    assert event["detail"]["reason"] == reason
    assert event["detail"]["position_id"] == state["position"]["id"]
    review = status["paper_review"][0]
    assert review["closed_trades"] == 1 and review["partially_exited_trades"] == 0
    assert D(review["closed_net_pnl"]) == D(event["detail"]["pnl"])


def test_partial_exits_count_as_one_trade_only_when_flat(desk):
    service, market, _analyst, clock = desk
    state = warm(desk)
    original_quantity = state["position"]["quantity"]
    service.stop("test")
    market.size = 30
    clock[0] += timedelta(minutes=1)
    service.run_once()
    review = service.status()["paper_review"][0]
    assert original_quantity > 30
    assert review["closed_trades"] == 0 and review["partially_exited_trades"] == 1
    assert D(review["closed_net_pnl"]) == 0
    market.size = 600
    clock[0] += timedelta(minutes=1)
    service.run_once()
    status = service.status()
    review = status["paper_review"][0]
    assert review["closed_trades"] == 1 and review["partially_exited_trades"] == 0
    assert D(review["closed_net_pnl"]) == D(status["session"]["realized_pnl"])


def test_underlying_outage_does_not_disable_premium_stop(desk):
    service, market, _analyst, clock = desk
    warm(desk)

    def unavailable(now):
        raise ValueError("Underlying unavailable")

    market.latest_underlying = unavailable
    market.bid = "90"
    clock[0] += timedelta(minutes=1)
    service.run_once()
    assert service.status()["session"]["position"] is None


def test_plan_generation_uses_receipt_clock_after_network_io(desk):
    _service, market, _analyst, clock = desk
    original = market.snapshot

    def delayed(now):
        snapshot = original(now)
        snapshot["candidates"][0]["quote"]["stamp"] = (now + timedelta(seconds=1)).isoformat()
        clock[0] += timedelta(seconds=2)
        return snapshot

    market.snapshot = delayed
    state = warm(desk)
    assert state["position"] is not None
    assert state["position"]["entry_plan"]["created_at"] == clock[0].isoformat()


def test_completed_day_becomes_bounded_next_day_learning_context(desk):
    service, _market, analyst, clock = desk
    first = warm(desk)
    service.stop("test")
    clock[0] += timedelta(minutes=1)
    service.run_once()
    status = service.status()
    assert status["session"]["state"] == "completed"
    assert status["learning"]["recent_days"][0]["day"] == first["day"]
    assert status["learning"]["recent_days"][0]["summary"]["training"] is False
    next_day = clock[0] + timedelta(days=1)
    clock[0] = next_day
    service.start(100000, 5, 10, "test")
    for i in range(5):
        clock[0] = next_day + timedelta(minutes=i)
        service.run_once()
    learning = analyst.last_snapshot["learning_context"]
    assert learning["mode"] == "bounded_in_context_evidence_not_model_training"
    assert len(learning["recent_days"]) == 1
    assert learning["playbook_evidence"][0]["closed_trades"] == 1
    assert learning["playbook_evidence"][0]["evidence_state"] == "collecting"
    assert learning["playbook_evidence"][0]["promotion_eligible"] is False
