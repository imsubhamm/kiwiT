BEGIN;
CREATE TABLE banknifty_learning_days (
    trading_date date PRIMARY KEY,
    selector_version text NOT NULL,
    summary jsonb NOT NULL,
    finalized_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO schema_migrations(version,name) VALUES(11,'banknifty_learning');
COMMIT;
