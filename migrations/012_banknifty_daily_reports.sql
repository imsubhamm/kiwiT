BEGIN;
CREATE TABLE banknifty_daily_reports (
    trading_date date PRIMARY KEY,
    generated_at timestamptz NOT NULL DEFAULT now(),
    report jsonb NOT NULL,
    delivery_status text NOT NULL DEFAULT 'pending'
        CHECK(delivery_status IN ('pending','sent','failed','not_configured')),
    delivery_attempts integer NOT NULL DEFAULT 0 CHECK(delivery_attempts >= 0),
    delivery_attempted_at timestamptz,
    delivery_error text NOT NULL DEFAULT ''
);
INSERT INTO schema_migrations(version,name) VALUES(12,'banknifty_daily_reports');
COMMIT;
