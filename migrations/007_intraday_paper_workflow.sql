BEGIN;

CREATE TABLE IF NOT EXISTS intraday_quotes (
    quote_id            bigserial PRIMARY KEY,
    symbol              text NOT NULL,
    exchange            text NOT NULL DEFAULT 'NSE',
    observed_at         timestamptz NOT NULL,
    last_price          numeric(24, 8) NOT NULL CHECK (last_price > 0),
    bid_price           numeric(24, 8) NOT NULL CHECK (bid_price > 0),
    ask_price           numeric(24, 8) NOT NULL CHECK (ask_price > 0),
    source              text NOT NULL,
    source_payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    UNIQUE (symbol, exchange, observed_at)
);

CREATE INDEX IF NOT EXISTS intraday_quotes_symbol_time_idx
ON intraday_quotes(symbol, exchange, observed_at DESC);

CREATE TABLE IF NOT EXISTS intraday_signals (
    signal_id           uuid PRIMARY KEY,
    account_id          text NOT NULL REFERENCES paper_accounts(account_id),
    strategy_id         text NOT NULL,
    strategy_version    text NOT NULL,
    symbol              text NOT NULL,
    exchange            text NOT NULL DEFAULT 'NSE',
    regime              text NOT NULL,
    pattern             text NOT NULL,
    side                text NOT NULL CHECK (side IN ('buy', 'sell')),
    signal_at           timestamptz NOT NULL,
    expires_at          timestamptz NOT NULL,
    entry_price         numeric(24, 8) NOT NULL CHECK (entry_price > 0),
    stop_price          numeric(24, 8) NOT NULL CHECK (stop_price > 0),
    target_price        numeric(24, 8) NOT NULL CHECK (target_price > 0),
    quantity            integer NOT NULL CHECK (quantity > 0),
    rationale           jsonb NOT NULL,
    status              text NOT NULL CHECK (status IN
                           ('pending','approved','rejected','expired','entered','exited','blocked')),
    reviewed_by         text,
    reviewed_at         timestamptz,
    review_reason       text,
    entry_fill_price    numeric(24, 8),
    entry_filled_at     timestamptz,
    exit_fill_price     numeric(24, 8),
    exit_filled_at      timestamptz,
    exit_reason         text,
    realized_pnl        numeric(24, 8),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now(),
    CHECK (expires_at > signal_at),
    CHECK (stop_price < entry_price AND target_price > entry_price)
);

CREATE UNIQUE INDEX IF NOT EXISTS intraday_signal_dedup_idx
ON intraday_signals(account_id, strategy_id, strategy_version, symbol, date(signal_at AT TIME ZONE 'Asia/Kolkata'), pattern)
WHERE side = 'buy';
CREATE INDEX IF NOT EXISTS intraday_signals_status_idx ON intraday_signals(status, signal_at DESC);

CREATE TABLE IF NOT EXISTS intraday_audit_events (
    event_id            uuid PRIMARY KEY,
    signal_id           uuid REFERENCES intraday_signals(signal_id),
    account_id          text NOT NULL,
    event_type          text NOT NULL,
    actor               text NOT NULL,
    event_at            timestamptz NOT NULL,
    details             jsonb NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS intraday_audit_signal_idx ON intraday_audit_events(signal_id, event_at);

CREATE TABLE IF NOT EXISTS intraday_worker_runs (
    run_id              uuid PRIMARY KEY,
    started_at          timestamptz NOT NULL,
    completed_at        timestamptz,
    state               text NOT NULL,
    quotes_ingested     integer NOT NULL DEFAULT 0,
    signals_created     integer NOT NULL DEFAULT 0,
    exits_created       integer NOT NULL DEFAULT 0,
    detail              text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS notification_deliveries (
    delivery_id         uuid PRIMARY KEY,
    signal_id           uuid REFERENCES intraday_signals(signal_id),
    channel             text NOT NULL,
    recipient           text NOT NULL,
    status              text NOT NULL CHECK (status IN ('sent','failed','not_configured')),
    attempted_at        timestamptz NOT NULL,
    error_message       text NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS daily_reconciliations (
    account_id          text NOT NULL REFERENCES paper_accounts(account_id),
    trading_date        date NOT NULL,
    state               text NOT NULL,
    open_positions      integer NOT NULL,
    pending_signals     integer NOT NULL,
    entries             integer NOT NULL,
    exits               integer NOT NULL,
    realized_pnl        numeric(24, 8) NOT NULL DEFAULT 0,
    reconciled_at       timestamptz NOT NULL,
    detail              jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY(account_id, trading_date)
);

DROP TRIGGER IF EXISTS intraday_audit_append_only ON intraday_audit_events;
CREATE TRIGGER intraday_audit_append_only BEFORE UPDATE OR DELETE ON intraday_audit_events
FOR EACH ROW EXECUTE FUNCTION prevent_mutation();

INSERT INTO schema_migrations(version, name) VALUES (7, 'intraday_paper_workflow')
ON CONFLICT (version) DO UPDATE SET name = EXCLUDED.name;
COMMIT;
