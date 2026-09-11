import json
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime, timedelta

import pytest

from kiwit.brokers.groww import BrokerApiError
from kiwit.marketdata.canonical import IST, Instrument, Session, TradingCalendar
from kiwit.marketdata.live import (
    GrowwMarketDataService,
    MarketDataUnavailable,
    ObservationStore,
    ReadRetryTransport,
)

NOW = datetime(2026, 9, 10, 9, 18, tzinfo=IST)
CALENDAR = TradingCalendar("test", (Session(NOW.date()),))


class Client:
    failure = False

    def quote(self, *args, **kwargs):
        if self.failure:
            raise BrokerApiError("SECRET_TOKEN")
        return {"timestamp": NOW.isoformat(), "last_price": 100}

    def banknifty_candles(self, start, end):
        return {
            "interval_in_minutes": 1,
            "candles": [[(NOW - timedelta(minutes=i)).isoformat(), 100, 102, 99, 101, None] for i in (3, 2, 1)],
        }


def service(tmp_path, client=None, clock=lambda: NOW):
    return GrowwMarketDataService(client or Client(), ObservationStore(tmp_path / "market.db"), CALENDAR, clock=clock)


def test_quote_persistence_idempotency_and_secrets(tmp_path):
    client = Client()
    svc = service(tmp_path, client)
    for _ in range(2):
        assert svc.quote(Instrument("NIFTYBEES")).require_valid().quote.ltp == 100
    with sqlite3.connect(tmp_path / "market.db") as db:
        rows = db.execute("SELECT payload FROM market_observations").fetchall()
    assert len(rows) == 1
    assert json.loads(rows[0][0])["schema_version"] == "market-v1"
    client.failure = True
    failed = svc.quote(Instrument("NIFTYBEES"))
    assert failed.quote is None
    with pytest.raises(MarketDataUnavailable) as caught:
        failed.require_valid()
    assert "SECRET_TOKEN" not in repr(failed) + str(caught.value)


def test_stale_quote_uses_receipt_clock(tmp_path):
    svc = service(tmp_path, clock=lambda: NOW + timedelta(minutes=2))
    assert svc.quote(Instrument("NIFTYBEES")).reasons == ("STALE_QUOTE",)


def test_live_candles_complete_session_and_persist(tmp_path):
    svc = service(tmp_path)
    result = svc.banknifty_candles(NOW - timedelta(minutes=3), NOW)
    assert len(result.require_valid().candles) == 3
    assert svc.banknifty_candles(NOW - timedelta(minutes=2), NOW).invalid_market_state
    with sqlite3.connect(tmp_path / "market.db") as db:
        assert db.execute("SELECT count(*) FROM market_observations").fetchone()[0] == 3


def test_storage_failure_cannot_return_valid_state(tmp_path):
    svc = service(tmp_path)
    with sqlite3.connect(tmp_path / "market.db") as db:
        db.execute("DROP TABLE market_observations")
    result = svc.quote(Instrument("NIFTYBEES"))
    assert result.invalid_market_state
    assert result.quote is None


@pytest.mark.parametrize(
    "failure",
    [429, 503, TimeoutError("private"), urllib.error.HTTPError("https://api.groww.in", 429, "private", {}, None)],
)
def test_transient_read_retried_without_fabrication(failure):
    calls, sleeps = [], []

    def transport(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            if isinstance(failure, Exception):
                raise failure
            return failure, b"private"
        return 200, b"real"

    retry = ReadRetryTransport(transport, sleep=sleeps.append)
    assert retry(urllib.request.Request("https://api.groww.in"), 8) == (200, b"real")
    assert len(calls) == 2
    assert 0.5 in sleeps


def test_retry_exhaustion_and_auth_failures_and_post_not_retried():
    for status, method, expected in [(503, "GET", 3), (401, "GET", 1), (503, "POST", 1)]:
        calls = []

        def transport(request, timeout, calls=calls, status=status):
            calls.append(request)
            return status, b"error"

        retry = ReadRetryTransport(transport, sleep=lambda _: None)
        assert retry(urllib.request.Request("https://api.groww.in", method=method), 8)[0] == status
        assert len(calls) == expected
