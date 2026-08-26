import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from uuid import uuid4

import pytest

from kiwit.intraday import IntradayService, _quote_time
from kiwit.paper_session import BoundDatabase, session_quantity, validate_limits


@pytest.mark.parametrize(
    "values",
    [
        (0, 5, 10),
        (100, 0, 10),
        (100, 5, 0),
        (100, 26, 10),
        (100, 5, 101),
        ("NaN", 5, 10),
        ("Infinity", 5, 10),
        (100.001, 5, 10),
    ],
)
def test_invalid_session_limits(values):
    with pytest.raises(ValueError):
        validate_limits(*values)


def test_deterministic_sizing_never_forces_one_share():
    assert session_quantity(D(10), D(0), D(0), D(10), D(100), D(5)) == 0
    qty = session_quantity(D(10000), D(-499), D(9000), D(1000), D(100), D(5))
    assert qty * D("100.1") <= D(501)


def test_timestamp_missing_does_not_become_fresh():
    with pytest.raises(ValueError, match="timestamp"):
        _quote_time({}, datetime.now(UTC))


@pytest.fixture(scope="module")
def connection():
    """Opt-in PostgreSQL tests: random private schema, all changes rolled back."""
    url = os.getenv("KIWIT_TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set KIWIT_TEST_DATABASE_URL for isolated PostgreSQL session tests")
    import psycopg
    from psycopg import sql

    with psycopg.connect(url) as conn, conn.transaction(force_rollback=True):
        schema = "kiwit_session_test_" + uuid4().hex
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {},pg_catalog").format(sql.Identifier(schema)))
        for path in sorted(Path("migrations").glob("*.sql")):
            migration = path.read_text().strip()
            assert migration.startswith("BEGIN;") and migration.endswith("COMMIT;")
            conn.execute(migration[6:-7])
        yield conn


@pytest.fixture
def service(connection):
    with connection.transaction(force_rollback=True):
        connection.execute(
            "INSERT INTO paper_accounts(account_id,initial_cash,cash_balance,status) VALUES('kiwit-paper-main',100000,100000,'active')"
        )
        yield IntradayService(BoundDatabase(connection), object())


NOW = datetime(2026, 8, 26, 5, 0, tzinfo=UTC)


def seed_setup(connection, now=NOW):
    for i in range(20):
        price = D(100) + D(i) / 10
        connection.execute(
            "INSERT INTO intraday_quotes(symbol,observed_at,last_price,bid_price,ask_price,source) VALUES('NIFTYBEES',%s,%s,%s,%s,'fixture') ON CONFLICT DO NOTHING",
            (now - timedelta(minutes=19 - i), price, price, price),
        )


def entered(service, connection, loss=5, profit=10):
    service.start_session(10000, loss, profit, "test-operator", NOW)
    seed_setup(connection)
    sid = service._create_signal("NIFTYBEES", NOW)
    assert sid
    service.session_tick(NOW)
    assert service.get_signal(sid)["status"] == "entered"
    return sid


def test_run_auto_entry_exit_and_duplicate_requests(service, connection):
    sid = entered(service, connection)
    session = service.start_session(10000, 5, 10, "test-operator", NOW)
    service.session_tick(NOW)
    assert session["entries"] == 1
    with pytest.raises(ValueError, match="immutable"):
        service.start_session(20000, 5, 10, "test-operator", NOW)
    with pytest.raises(ValueError, match="already entered"):
        service.review(sid, True, "test", "test", NOW)
    assert service._create_signal("NIFTYBEES", NOW) is None
    service.stop_session("test-operator", NOW)
    service.session_tick(NOW)
    result = service.get_signal(sid)
    assert result["status"] == "exited"
    assert D(result["realized_pnl"]) < 0  # spread/slippage + costs, no fake zero-cost profit
    assert service.session_status()["state"] == "completed"
    assert connection.execute("SELECT sum(quantity) FROM paper_positions").fetchone()[0] == 0
    cash, pnl = connection.execute("SELECT cash_balance,realized_pnl FROM paper_accounts WHERE account_id='kiwit-paper-main'").fetchone()
    assert abs(cash - D(100000) - pnl) < D(".000001")
    assert service.start_session(10000, 5, 10, "test-operator", NOW)["state"] == "completed"


def test_stop_with_stale_quotes_remains_unresolved(service, connection):
    sid = entered(service, connection)
    later = NOW + timedelta(minutes=5)
    service.stop_session("test", later)
    service.session_tick(later)
    assert service.session_status()["state"] == "stopping"
    assert service.get_signal(sid)["status"] == "entered"
    assert "waiting for fresh" in service.session_status()["detail"]


def test_session_profit_and_loss_use_mark_to_market(service, connection):
    sid = entered(service, connection, loss=1, profit=1)
    connection.execute(
        "INSERT INTO intraday_quotes(symbol,observed_at,last_price,bid_price,ask_price,source) VALUES('NIFTYBEES',%s,120,120,120,'fixture')",
        (NOW + timedelta(seconds=30),),
    )
    service.session_tick(NOW + timedelta(seconds=30))
    assert service.get_signal(sid)["exit_reason"] == "session_profit_target"
    assert service.session_status()["state"] == "completed"


def test_gap_loss_stops_run_and_does_not_guarantee_limit(service, connection):
    sid = entered(service, connection, loss=1)
    connection.execute(
        "INSERT INTO intraday_quotes(symbol,observed_at,last_price,bid_price,ask_price,source) VALUES('NIFTYBEES',%s,80,80,80,'fixture')",
        (NOW + timedelta(seconds=30),),
    )
    service.session_tick(NOW + timedelta(seconds=30))
    assert service.get_signal(sid)["exit_reason"] == "session_loss_limit"
    assert D(service.session_status()["pnl"]) < -100


def test_end_of_day_flattens_before_close(service, connection):
    sid = entered(service, connection)
    later = NOW.replace(hour=9, minute=40)  # 15:10 IST
    connection.execute(
        "INSERT INTO intraday_quotes(symbol,observed_at,last_price,bid_price,ask_price,source) VALUES('NIFTYBEES',%s,102,102,102,'fixture')",
        (later,),
    )
    service.session_tick(later)
    assert service.get_signal(sid)["exit_reason"] == "session_end_of_day"
    assert service.session_status()["state"] == "completed"


def test_manual_cannot_bypass_run_consent(service, connection):
    service.start_session(10000, 5, 10, "test", NOW)
    seed_setup(connection)
    sid = service._create_signal("NIFTYBEES", NOW)
    with pytest.raises(ValueError, match="Run owns"):
        service.review(sid, True, "test", "manual", NOW)


def test_run_fails_without_cash_or_after_cutoff(service):
    with pytest.raises(ValueError, match="cash"):
        service.start_session(200000, 5, 10, "test", NOW)
    with pytest.raises(ValueError, match="15:00"):
        service.start_session(10000, 5, 10, "test", NOW.replace(hour=10))


def test_previous_day_quotes_do_not_produce_signals(service, connection):
    service.start_session(10000, 5, 10, "test", NOW)
    seed_setup(connection, NOW - timedelta(days=1))
    assert service._create_signal("NIFTYBEES", NOW) is None


def test_restart_preserves_approval_without_double_fill(service, connection):
    sid = entered(service, connection)
    restarted = IntradayService(BoundDatabase(connection), object())
    restarted.session_tick(NOW)
    assert restarted.session_status()["entries"] == 1
    assert restarted.get_signal(sid)["status"] == "entered"


def test_completed_run_does_not_restart_manual_observer(service, connection):
    entered(service, connection)
    service.stop_session("test", NOW)
    service.session_tick(NOW)
    assert service._create_signal("NIFTYBEES", NOW + timedelta(minutes=6)) is None


def test_budget_too_small_never_creates_fill(service, connection):
    service.start_session(10, 5, 10, "test", NOW)
    seed_setup(connection)
    sid = service._create_signal("NIFTYBEES", NOW)
    service.session_tick(NOW)
    assert service.get_signal(sid)["status"] == "pending"
    assert "Budget too small" in service.session_status()["detail"]
    assert service.session_status()["entries"] == 0


def test_future_quote_blocks_entry(service, connection):
    service.start_session(10000, 5, 10, "test", NOW)
    seed_setup(connection)
    sid = service._create_signal("NIFTYBEES", NOW)
    connection.execute(
        "INSERT INTO intraday_quotes(symbol,observed_at,last_price,bid_price,ask_price,source) VALUES('NIFTYBEES',%s,102,102,102,'fixture')",
        (NOW + timedelta(minutes=5),),
    )
    service.session_tick(NOW)
    assert service.get_signal(sid)["status"] == "pending"
    assert service.session_status()["entries"] == 0


def test_scheduled_worker_runs_whole_flow_without_per_trade_approval(service, connection):
    service.start_session(10000, 5, 10, 'test', NOW)
    seed_setup(connection)

    class Feed:
        def quote(self, symbol):
            return {'last_price':101.9, 'bid_price':101.9, 'offer_price':101.9,
                    'last_trade_time':int(NOW.timestamp())}

    service.broker = Feed()
    result = service.run_once(NOW)
    assert result['state'] == 'completed', result
    assert service.session_status()['entries'] == 1
    assert service.session_status()['open_positions'] == 1
    service.stop_session('test',NOW)
    result = service.run_once(NOW)
    assert result['state'] == 'completed', result
    assert service.session_status()['state'] == 'completed'
    assert service.session_status()['open_positions'] == 0


def test_auto_account_is_isolated_and_requires_run(service, connection):
    entered(service,connection)  # old account can retain its holding
    automatic=IntradayService(BoundDatabase(connection),object(),replace(service.settings,account_id='kiwit-paper-auto'))
    assert automatic._create_signal('NIFTYBEES',NOW) is None
    assert automatic.start_session(10000,5,10,'test',NOW)['state'] == 'armed'
    sid=automatic._create_signal('NIFTYBEES',NOW)
    automatic.session_tick(NOW)
    assert automatic.get_signal(sid)['status'] == 'entered'
    assert len(automatic.list_signals()['signals']) == 1
