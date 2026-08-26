import json
import os
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import pytest

from kiwit.banknifty import BankNiftyService, quantity_for
from kiwit.options_ai import parse_response, request_body
from kiwit.options_market import completed_candles, contracts_from_csv, executable_quote
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
                            decision or {"action": "HOLD", "symbol": "", "strategy": "no_trade", "summary": "Wait"}
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

    def snapshot(self, now):
        if self.fail:
            raise ValueError("stale")
        return {
            "at": now.isoformat(),
            "spot_at": now.isoformat(),
            "spot": "55000",
            "candidates": [dict(CONTRACT, quote=self.quote(CONTRACT["symbol"], now))],
        }


class Analyst:
    def __init__(self):
        self.calls = 0
        self.fail = False
        self.action = "BUY"

    def decide(self, snapshot):
        self.calls += 1
        if self.fail:
            raise TimeoutError()
        return {
            "action": self.action,
            "symbol": CONTRACT["symbol"],
            "strategy": "momentum",
            "summary": "Fixture only",
        }, {"input_tokens": 100, "output_tokens": 50, "budget_charge_usd": ".002"}


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
