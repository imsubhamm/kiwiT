import json
import logging
import sys
from types import SimpleNamespace
from unittest.mock import Mock

from kiwit.observability import JsonFormatter, Metrics
from kiwit.operational_readiness import inspect_readiness


def test_logs_redact_environment_bearer_and_exception_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "private-openai-value")
    monkeypatch.setenv("KIWIT_DATABASE_URL", "postgresql://user:secret@host/db")
    try:
        raise RuntimeError("unknown-credential-from-upstream")
    except RuntimeError:
        record = logging.LogRecord("kiwit", logging.ERROR, "", 0,
                                   "private-openai-value Bearer groww-token postgresql://user:secret@host/db",
                                   (), sys.exc_info())
    output = JsonFormatter().format(record)
    for secret in ("private-openai-value", "groww-token", "user:secret", "unknown-credential-from-upstream"):
        assert secret not in output
    assert json.loads(output)["exception_type"] == "RuntimeError"


def test_readiness_reports_failure_without_exception_text():
    database = Mock()
    database.healthcheck.side_effect = RuntimeError("database-password-secret")
    report = inspect_readiness(database, None)
    assert report["status"] == "degraded"
    assert report["new_execution_allowed"] is False
    assert "database-password-secret" not in json.dumps(report)
    assert "DATABASE_OR_HALT_CHECK_FAILED" in report["reason_codes"]


def test_active_halt_and_stale_data_have_explicit_reasons():
    database = Mock()
    database.healthcheck.return_value = {"schema_version": 12}
    # Use an explicit context manager rather than MagicMock's implicit state.
    from contextlib import nullcontext
    cursor = Mock()
    cursor.execute.return_value.fetchall.return_value = [("global", "OPERATOR_HALT")]
    database.connect.return_value = nullcontext(cursor)
    service = SimpleNamespace(freshness=lambda now: {"instruments": [{"fresh": False}], "worker": {"state": "failed"}})
    report = inspect_readiness(database, service)
    assert report["checks"]["kill_switch"] == "HALTED"
    assert "CASH_WORKER_FAILED_OR_DEGRADED" in report["reason_codes"]
    assert "ACTIVE_EXECUTION_HALT" in report["reason_codes"]
    assert report["new_execution_allowed"] is False


def test_error_latency_metrics_are_retained():
    metrics = Metrics()
    metrics.begin()
    metrics.finish("GET", "/ready", 503, .25)
    rendered = metrics.render()
    assert 'status="503"} 1' in rendered
    assert 'route="/ready"} 0.250000' in rendered
