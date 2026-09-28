"""KIW-43 pre-market Groww approval readiness coverage."""
# ruff: noqa: F811
from datetime import timedelta

from test_banknifty import NOW, Market, db  # noqa: F401

from kiwit.banknifty import BankNiftyService, broker_readiness_transition
from kiwit.brokers.groww import BrokerApiError
from kiwit.intraday import SignalMailer
from kiwit.options_operations import diagnostics


class ReadinessBroker:
    def __init__(self, error=None):
        self.error = error
        self.profile_calls = 0
        self.quote_calls = 0
        self.order_calls = 0

    def profile(self):
        self.profile_calls += 1
        if self.error:
            raise self.error
        return {"nse_enabled": True}

    def quote(self, symbol, segment="CASH"):
        self.quote_calls += 1
        assert (symbol, segment) == ("NIFTYBEES", "CASH")
        return {"last_price": 250.25}

    def place_order(self, _order):
        self.order_calls += 1
        raise AssertionError("Readiness must never place an order")


class ReadinessMailer:
    configured = True

    def __init__(self):
        self.alerts = []

    def send_broker_readiness(self, **alert):
        self.alerts.append(alert)
        return "sent", ""


def test_readiness_lifecycle_retains_first_failure_count_and_recovery():
    first = broker_readiness_transition(None, NOW, "failed")
    assert first["first_failed_at"] == NOW
    assert first["failure_count"] == 1 and first["notify"] == "failed"
    second = broker_readiness_transition(first, NOW + timedelta(minutes=1), "failed")
    assert second["first_failed_at"] == NOW
    assert second["failure_count"] == 2 and second["notify"] is None
    recovered_at = NOW + timedelta(minutes=2)
    recovered = broker_readiness_transition(second, recovered_at, "ready")
    assert recovered["first_failed_at"] == NOW
    assert recovered["failure_count"] == 2
    assert recovered["recovered_at"] == recovered_at and recovered["notify"] == "recovered"
    stable = broker_readiness_transition(recovered, NOW + timedelta(minutes=3), "ready")
    assert stable["recovered_at"] == recovered_at and stable["notify"] is None


def test_readiness_notifications_are_actionable_and_confirm_recovery():
    messages = []
    mailer = SignalMailer()
    mailer.host = "smtp.example"
    mailer.sender = "alerts@example.test"
    mailer.recipients = ("ops@example.test",)
    mailer._send = lambda message: messages.append(message) or ("sent", "")
    failure = mailer.send_broker_readiness(
        status="failed", occurred_at=NOW, reason_code="GROWW_SESSION_APPROVAL_REQUIRED",
        first_failed_at=NOW, failure_count=1, recovered_at=None,
        dashboard_url="https://kiwit.example/dashboard",
    )
    recovery = mailer.send_broker_readiness(
        status="recovered", occurred_at=NOW + timedelta(minutes=3),
        reason_code="GROWW_READINESS_RECOVERED", first_failed_at=NOW, failure_count=3,
        recovered_at=NOW + timedelta(minutes=3), dashboard_url="https://kiwit.example/dashboard",
    )
    assert failure[0] == recovery[0] == "sent"
    assert "Groww session approval required" in messages[0]["Subject"]
    assert "approve today’s API session" in messages[0].get_content()
    assert "approval and quote ready" in messages[1]["Subject"]
    assert "No broker order was placed" in messages[1].get_content()


def test_missing_approval_provider_failure_and_successful_recovery(db):
    clock = [NOW.replace(hour=3, minute=30)]  # 09:00 IST, before the 09:20 observer.
    broker = ReadinessBroker(BrokerApiError("Groww session approval is required"))
    mailer = ReadinessMailer()
    service = BankNiftyService(db, broker, clock=lambda: clock[0], mailer=mailer)

    first = service.check_broker_readiness()
    assert first["state"] == "failed"
    assert first["reason_code"] == "GROWW_SESSION_APPROVAL_REQUIRED"
    assert first["safe_message"] == "Groww session approval required"
    clock[0] += timedelta(minutes=1)
    second = service.check_broker_readiness()
    assert second["failure_count"] == 2
    assert len(mailer.alerts) == 1

    broker.error = None
    clock[0] += timedelta(minutes=1)
    recovered = service.check_broker_readiness()
    assert recovered["state"] == "ready"
    assert recovered["failure_count"] == 2
    assert recovered["recovered_at"] == clock[0]
    assert [alert["status"] for alert in mailer.alerts] == ["failed", "recovered"]
    assert broker.quote_calls == 1 and broker.order_calls == 0

    with db.transaction() as connection:
        row = connection.execute(
            "SELECT status,first_failed_at,failure_count,recovered_at,failure_alert_status,recovery_alert_status "
            "FROM banknifty_broker_readiness"
        ).fetchone()
    assert row == ("ready", first["first_failed_at"], 2, clock[0], "sent", "sent")
    readiness = diagnostics(service.store, clock[0])["broker_readiness"]
    assert readiness["status"] == "ready"
    assert readiness["failure_count"] == 2
    assert "GROWW_SESSION_APPROVAL_REQUIRED" not in diagnostics(service.store, clock[0])["reason_codes"]


def test_provider_error_is_distinct_and_does_not_attempt_quote_or_order(db):
    broker = ReadinessBroker(BrokerApiError("Groww request rejected with HTTP 500"))
    service = BankNiftyService(db, broker, clock=lambda: NOW.replace(hour=3, minute=30), mailer=ReadinessMailer())
    result = service.check_broker_readiness()
    assert result["reason_code"] == "GROWW_PROVIDER_ERROR"
    assert result["safe_message"] == "Groww provider readiness check failed"
    assert broker.quote_calls == 0 and broker.order_calls == 0


def test_first_successful_observation_clears_a_premarket_approval_alert(db):
    clock = [NOW.replace(hour=3, minute=30)]
    broker = ReadinessBroker(BrokerApiError("Groww session approval is required"))
    mailer = ReadinessMailer()
    service = BankNiftyService(db, broker, market=Market(), clock=lambda: clock[0], mailer=mailer)
    assert service.check_broker_readiness()["state"] == "failed"
    broker.error = None
    clock[0] = NOW
    assert service.observe()["state"] == "observed"
    readiness = diagnostics(service.store, clock[0])["broker_readiness"]
    assert readiness["status"] == "ready"
    assert readiness["recovered_at"] == NOW.isoformat()
    assert [alert["status"] for alert in mailer.alerts] == ["failed", "recovered"]
    assert broker.order_calls == 0
