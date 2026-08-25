BEGIN;

CREATE TABLE IF NOT EXISTS paper_accounts (
    account_id          text PRIMARY KEY,
    currency            char(3) NOT NULL DEFAULT 'INR',
    initial_cash        numeric(24, 8) NOT NULL CHECK (initial_cash > 0),
    cash_balance        numeric(24, 8) NOT NULL CHECK (cash_balance >= 0),
    realized_pnl        numeric(24, 8) NOT NULL DEFAULT 0,
    status              text NOT NULL CHECK (status IN ('active', 'halted', 'closed')),
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS human_reviews (
    review_id           uuid PRIMARY KEY,
    proposal_id         uuid NOT NULL REFERENCES trade_proposals(proposal_id),
    approved            boolean NOT NULL,
    reviewer            text NOT NULL,
    reason              text NOT NULL DEFAULT '',
    reviewed_at         timestamptz NOT NULL,
    UNIQUE (proposal_id)
);

ALTER TABLE broker_orders ADD COLUMN IF NOT EXISTS account_id text REFERENCES paper_accounts(account_id);

CREATE TABLE IF NOT EXISTS paper_positions (
    account_id          text NOT NULL REFERENCES paper_accounts(account_id),
    instrument_id       uuid NOT NULL REFERENCES instruments(instrument_id),
    quantity            numeric(30, 8) NOT NULL CHECK (quantity >= 0),
    average_price       numeric(24, 8) NOT NULL CHECK (average_price >= 0),
    realized_pnl        numeric(24, 8) NOT NULL DEFAULT 0,
    updated_at          timestamptz NOT NULL,
    PRIMARY KEY (account_id, instrument_id),
    CHECK ((quantity = 0 AND average_price = 0) OR quantity > 0)
);

CREATE TABLE IF NOT EXISTS paper_daily_ledger (
    account_id          text NOT NULL REFERENCES paper_accounts(account_id),
    trading_date        date NOT NULL,
    starting_equity     numeric(24, 8) NOT NULL CHECK (starting_equity >= 0),
    realized_pnl        numeric(24, 8) NOT NULL DEFAULT 0,
    fees                numeric(24, 8) NOT NULL DEFAULT 0 CHECK (fees >= 0),
    turnover            numeric(30, 8) NOT NULL DEFAULT 0 CHECK (turnover >= 0),
    trade_count         integer NOT NULL DEFAULT 0 CHECK (trade_count >= 0),
    updated_at          timestamptz NOT NULL,
    PRIMARY KEY (account_id, trading_date)
);

CREATE INDEX IF NOT EXISTS paper_orders_account_idx ON broker_orders (account_id, updated_at) WHERE environment = 'paper';
CREATE INDEX IF NOT EXISTS paper_positions_account_idx ON paper_positions (account_id) WHERE quantity > 0;

DROP TRIGGER IF EXISTS human_reviews_append_only ON human_reviews;
CREATE TRIGGER human_reviews_append_only BEFORE UPDATE OR DELETE ON human_reviews
FOR EACH ROW EXECUTE FUNCTION prevent_mutation();

INSERT INTO schema_migrations(version, name) VALUES (4, 'paper_trading')
ON CONFLICT (version) DO UPDATE SET name = EXCLUDED.name;
COMMIT;
