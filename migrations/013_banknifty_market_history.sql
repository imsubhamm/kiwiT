BEGIN;
CREATE TABLE banknifty_market_history (
    trading_date date NOT NULL,
    observed_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    spot numeric NOT NULL CHECK (spot > 0),
    market_snapshot jsonb NOT NULL,
    strategy_selection jsonb NOT NULL,
    scan_state text NOT NULL,
    PRIMARY KEY (trading_date, observed_at)
);
CREATE INDEX banknifty_market_history_recorded_at_idx ON banknifty_market_history(recorded_at DESC);
INSERT INTO schema_migrations(version,name) VALUES(13,'banknifty_market_history');
COMMIT;
