# Operations hardening (KIW-34)

The application retains public `/live`, `/health` and database-backed `/ready`, and authenticated `/metrics`. `/ready` answers service readiness, not strategy/model eligibility. A database failure returns 503. HTTP metrics retain error status counts, in-flight requests and cumulative latency by route.

The authenticated `/api/v1/operations/readiness` endpoint adds read-only diagnostics: database/schema availability, active halt scopes and reason codes, cash-worker errors, quote freshness, lifecycle configuration and release identity. It performs no Groww token refresh or trading mutations. Unknown V2 model readiness remains NOT_ASSESSED and `new_execution_allowed` is always false: downstream strategy, session and risk controls remain mandatory. Cash-market staleness outside market hours may be expected; this report does not automatically restart or unhalt the application.

## Logging and credentials

The JSON formatter redacts environment values whose names identify tokens, secrets, passwords, API keys or database URLs, plus bearer and credential assignment patterns. Exception records retain exception type without raw exception messages or traceback text. Request logs use registered route templates, avoiding user-controlled URL path segments. The intraday worker persists an exception type and generic failure explanation instead of raw upstream errors.

Credentials stay in the existing protected server environment/token cache; deployment preserves that environment. CI scans for committed private keys and runs dependency/security checks. Do not embed credentials in arbitrary diagnostic messages: redaction is a defense in depth, not an authorization to log secrets. Third-party processes with independent log handlers require their own controls.

## Emergency halt semantics

The existing authenticated account halt endpoint also accepts `global` scope. A halt is persisted in `system_halts`. The general PostgreSQL paper-fill path now holds a SHARE table lock from its halt check through fill commit, matching the existing intraday/Bank Nifty lock pattern. Halt writes conflict with that lock: a fill already holding it may complete before the halt is acknowledged; after the halt commits, later new fills observe the halt. Existing duplicate fills remain idempotent reads. This does not cancel already-completed fills or pretend to flatten positions without quotes.

Existing freshness checks, database transactions, model/risk gates and the KIW-33 restart/manual-RUN behavior remain in place. This build does not enable live broker mutations, activate the opt-in scheduler or deploy automatically.

Tests cover secret/exception redaction, authenticated diagnostics, explicit degraded reasons and HTTP error/latency counters. Full operational recovery and kill-switch concurrency should also be exercised against the deployment's PostgreSQL instance in an isolated test account before production activation.
