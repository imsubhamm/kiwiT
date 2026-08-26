BEGIN;
CREATE TABLE banknifty_sessions (
    trading_date date PRIMARY KEY,
    state jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE banknifty_events (
    event_id bigserial PRIMARY KEY,
    trading_date date NOT NULL,
    event_at timestamptz NOT NULL DEFAULT now(),
    kind text NOT NULL,
    detail jsonb NOT NULL
);
CREATE TABLE banknifty_ai_calls (
    call_id uuid PRIMARY KEY,
    trading_date date NOT NULL,
    slot bigint NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now(),
    state text NOT NULL,
    reserved_usd numeric NOT NULL CHECK(reserved_usd >= 0),
    snapshot jsonb NOT NULL,
    result jsonb
);
INSERT INTO schema_migrations(version,name) VALUES(10,'banknifty_ai');
COMMIT;
