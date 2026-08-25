BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version             bigint PRIMARY KEY,
    applied_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS datasets (
    dataset_id          uuid PRIMARY KEY,
    name                text NOT NULL,
    source_type         text NOT NULL CHECK (source_type IN ('official_exchange', 'official_regulator', 'vendor', 'derived')),
    source_uri          text NOT NULL,
    content_sha256      char(64) NOT NULL,
    period_start        date,
    period_end          date,
    row_count           bigint NOT NULL CHECK (row_count >= 0),
    retrieved_at        timestamptz NOT NULL,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at          timestamptz NOT NULL DEFAULT now(),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS instruments (
    instrument_id       uuid PRIMARY KEY,
    exchange            text NOT NULL,
    symbol              text NOT NULL,
    series              text NOT NULL,
    isin                text,
    lot_size            integer NOT NULL CHECK (lot_size > 0),
    tick_size           numeric(20, 8) NOT NULL CHECK (tick_size > 0),
    valid_from          date NOT NULL,
    valid_to            date,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (exchange, symbol, series, valid_from)
);

CREATE TABLE IF NOT EXISTS daily_bars (
    dataset_id          uuid NOT NULL REFERENCES datasets(dataset_id),
    instrument_id       uuid NOT NULL REFERENCES instruments(instrument_id),
    trading_date        date NOT NULL,
    open_price          numeric(24, 8) NOT NULL CHECK (open_price > 0),
    high_price          numeric(24, 8) NOT NULL CHECK (high_price > 0),
    low_price           numeric(24, 8) NOT NULL CHECK (low_price > 0),
    close_price         numeric(24, 8) NOT NULL CHECK (close_price > 0),
    volume              numeric(30, 0),
    is_adjusted         boolean NOT NULL DEFAULT false,
    ingested_at         timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (dataset_id, instrument_id, trading_date),
    CHECK (high_price >= low_price),
    CHECK (high_price >= open_price AND high_price >= close_price),
    CHECK (low_price <= open_price AND low_price <= close_price)
);

CREATE TABLE IF NOT EXISTS corporate_actions (
    action_id           uuid PRIMARY KEY,
    instrument_id       uuid NOT NULL REFERENCES instruments(instrument_id),
    ex_date             date NOT NULL,
    action_type         text NOT NULL,
    ratio               numeric(24, 10),
    cash_amount         numeric(24, 8),
    source_dataset_id   uuid NOT NULL REFERENCES datasets(dataset_id),
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (instrument_id, ex_date, action_type)
);

CREATE TABLE IF NOT EXISTS index_membership (
    index_symbol        text NOT NULL,
    instrument_id       uuid NOT NULL REFERENCES instruments(instrument_id),
    valid_from          date NOT NULL,
    valid_to            date,
    source_dataset_id   uuid NOT NULL REFERENCES datasets(dataset_id),
    PRIMARY KEY (index_symbol, instrument_id, valid_from)
);

CREATE TABLE IF NOT EXISTS strategy_versions (
    strategy_id         text NOT NULL,
    version             text NOT NULL,
    status              text NOT NULL CHECK (status IN ('research', 'candidate', 'paper', 'approved', 'rejected', 'retired')),
    specification       jsonb NOT NULL,
    specification_sha256 char(64) NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (strategy_id, version),
    UNIQUE (specification_sha256)
);

CREATE TABLE IF NOT EXISTS backtest_runs (
    run_id              uuid PRIMARY KEY,
    run_fingerprint     char(64) NOT NULL UNIQUE,
    strategy_id         text NOT NULL,
    strategy_version    text NOT NULL,
    code_sha256         char(64) NOT NULL,
    parameters          jsonb NOT NULL,
    metrics             jsonb NOT NULL,
    started_at          timestamptz NOT NULL,
    completed_at        timestamptz NOT NULL,
    created_at          timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (strategy_id, strategy_version) REFERENCES strategy_versions(strategy_id, version),
    CHECK (completed_at >= started_at)
);

CREATE TABLE IF NOT EXISTS backtest_run_datasets (
    run_id              uuid NOT NULL REFERENCES backtest_runs(run_id),
    dataset_id          uuid NOT NULL REFERENCES datasets(dataset_id),
    role                text NOT NULL DEFAULT 'market_data',
    PRIMARY KEY (run_id, dataset_id, role)
);

CREATE TABLE IF NOT EXISTS backtest_trades (
    run_id              uuid NOT NULL REFERENCES backtest_runs(run_id),
    sequence_number     integer NOT NULL CHECK (sequence_number > 0),
    instrument_id       uuid REFERENCES instruments(instrument_id),
    signal_at           timestamptz,
    entered_at          timestamptz NOT NULL,
    exited_at           timestamptz NOT NULL,
    side                text NOT NULL CHECK (side IN ('buy', 'sell')),
    quantity            numeric(30, 8) NOT NULL CHECK (quantity > 0),
    entry_price         numeric(24, 8) NOT NULL,
    exit_price          numeric(24, 8) NOT NULL,
    pnl                 numeric(24, 8) NOT NULL,
    exit_reason         text NOT NULL,
    metadata            jsonb NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (run_id, sequence_number)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id            uuid PRIMARY KEY,
    occurred_at         timestamptz NOT NULL,
    event_type          text NOT NULL,
    aggregate_type      text NOT NULL,
    aggregate_id        text NOT NULL,
    payload             jsonb NOT NULL,
    previous_hash       char(64) NOT NULL,
    event_hash          char(64) NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS daily_bars_instrument_date_idx ON daily_bars (instrument_id, trading_date);
CREATE INDEX IF NOT EXISTS backtest_runs_strategy_idx ON backtest_runs (strategy_id, strategy_version, completed_at);
CREATE INDEX IF NOT EXISTS audit_events_aggregate_idx ON audit_events (aggregate_type, aggregate_id, occurred_at);

INSERT INTO schema_migrations(version) VALUES (1) ON CONFLICT (version) DO NOTHING;
COMMIT;
