BEGIN;

CREATE TABLE banknifty_broker_readiness (
    trading_date date PRIMARY KEY,
    status text NOT NULL CHECK(status IN ('ready','failed')),
    first_failed_at timestamptz,
    last_checked_at timestamptz NOT NULL,
    failure_count integer NOT NULL DEFAULT 0 CHECK(failure_count >= 0),
    reason_code text,
    safe_message text,
    recovered_at timestamptz,
    failure_alert_status text CHECK(failure_alert_status IN ('pending','sent','failed','not_configured')),
    failure_alert_attempts integer NOT NULL DEFAULT 0 CHECK(failure_alert_attempts >= 0),
    recovery_alert_status text CHECK(recovery_alert_status IN ('pending','sent','failed','not_configured')),
    recovery_alert_attempts integer NOT NULL DEFAULT 0 CHECK(recovery_alert_attempts >= 0)
);

INSERT INTO schema_migrations(version,name) VALUES(17,'broker_readiness');
COMMIT;
