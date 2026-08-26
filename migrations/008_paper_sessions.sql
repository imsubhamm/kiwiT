BEGIN;
CREATE TABLE paper_sessions (
    session_id uuid PRIMARY KEY,
    account_id text NOT NULL REFERENCES paper_accounts(account_id),
    trading_date date NOT NULL,
    amount numeric(24,8) NOT NULL CHECK(amount > 0),
    loss_pct numeric(8,4) NOT NULL CHECK(loss_pct > 0 AND loss_pct <= 25),
    profit_pct numeric(8,4) NOT NULL CHECK(profit_pct > 0 AND profit_pct <= 100),
    state text NOT NULL CHECK(state IN ('armed','running','stopping','completed')),
    approved_by text NOT NULL,
    approved_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL,
    detail text NOT NULL,
    router_version text NOT NULL DEFAULT 'observer-session-v1',
    UNIQUE(account_id,trading_date)
);
CREATE UNIQUE INDEX paper_sessions_one_active ON paper_sessions(account_id)
WHERE state IN ('armed','running','stopping');
ALTER TABLE intraday_signals ADD COLUMN session_id uuid REFERENCES paper_sessions(session_id);
CREATE INDEX intraday_signals_session ON intraday_signals(session_id);
DROP INDEX intraday_signal_dedup_idx;
CREATE UNIQUE INDEX intraday_signal_dedup_idx
ON intraday_signals(account_id,strategy_id,strategy_version,symbol,date(signal_at AT TIME ZONE 'Asia/Kolkata'),pattern)
WHERE side='buy' AND session_id IS NULL;
CREATE UNIQUE INDEX intraday_session_signal_dedup ON intraday_signals(session_id,symbol,signal_at)
WHERE session_id IS NOT NULL;
INSERT INTO schema_migrations(version,name) VALUES(8,'paper_sessions');
COMMIT;
