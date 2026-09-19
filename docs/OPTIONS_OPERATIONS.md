# Options desk operations and audit remediation

The active engine remains the deterministic Bank Nifty options paper desk. Generic
V2 ML/replay/RL modules are research components and are not silently enabled by this
release. Live broker orders remain disabled. No provider/model change is made.

## Runtime

- Observer: independently records read-only market snapshots during regular sessions,
  even without RUN or after a session completes. It never calls the analyst.
- Supervisor: monitors existing positions independently of the observer, SMTP and AI.
- Decision worker: consumes fresh recorded observations, runs the versioned selector,
  preflights the request, reserves budget, then validates any response before a fill.
- Reports worker: catches up missing reports and claims a five-minute delivery lease.
  Missing SMTP configuration consumes no attempts. Failures retry after 15 minutes.
  SMTP is at-least-once: a crash after server acceptance can duplicate an email.
- Watchdog: checks the options worker heartbeats and operational blockers as well as
  web readiness. Authenticated operations readiness and the dashboard expose them.

Annual regular-session calendar: NSE F&O circular FAOP71777, 2026. Source:
https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf . Unknown years block
entry; add the verified next-year calendar before year-end. Special-session times
must be implemented and reviewed before trading those sessions. Review exchange
amendments; this bundled calendar does not fetch notices automatically.

## Evidence and recovery

Selector v2/playbooks v2 retain the deliberately chosen no-fixed-holding-deadline
policy. Premium, underlying invalidation, operator, halt and end-of-day exits stay
active. A versioned experiment fingerprint includes model/provider/prompt/schema,
release, sizing, selector and exit policy. Learning includes only closed, same-day
trades from the exact experiment. Legacy unbound records remain visible but are not
used to improve the current experiment's learning statistics.

`banknifty_trade_outcomes` derives actual exit times from historical executable
quotes, including the September 1 position closed on September 15. No ledger rows
are rewritten. Recovery outcomes are excluded from ordinary intraday review and
learning and exposed separately. Expired residual contracts require explicit
verified settlement; the worker does not sell them at an unrelated future quote.
Existing immutable reports remain historical snapshots; the current recovery view
is authoritative for subsequent classification.

Structured failure records store category, dispatch certainty, HTTP status, safe
request ID, latency and provider/model. No raw provider body or credential is stored.
Local validation happens before reservation. Ambiguous dispatched requests retain
budget. Three successive failures open a 15-minute circuit, after which one new
eligible call can probe recovery. Independent exits continue throughout.

## Deployment and permissions

Apply migration 014 with the migration owner. Provision roles using
`scripts/provision_database_roles.py --output /protected/path/roles.env` with
`KIWIT_MIGRATION_DATABASE_URL` supplied securely. It creates `kiwit_runtime` and
`kiwit_reader`, writes generated URLs to an exclusive mode-0600 file, and fails if
PUBLIC schema CREATE privileges would defeat the restriction. It never prints URLs.
Put only the runtime URL in `/etc/kiwit/kiwit.env`. Keep the migration URL in a
root-only `/etc/kiwit/migration.env`; services never source that file. The deployment
script refreshes runtime grants after migrations when the owner URL is configured.
Validate login, session writes, halts, workers, migrations and rollback before release.

All deployment packages are constrained by `requirements.lock`, shared with CI.
Migrations are additive; older code ignores the new columns/view/table. Rollback
restores or stops the new worker units along with the application. No historical
performance or database role is silently modified by an application import.

## Verification still requiring external evidence

Historical index bars cannot supply missing historical option bid/ask/depth. Real
walk-forward profitability, paired AI comparisons, learned ML policies and RL
validation require adequate datasets and time; unit tests do not complete them.
Keep these as explicit evidence gates. Before treating backup requirements as met,
verify managed database PITR retention and perform an isolated restore drill. The
repository cannot infer service-plan features from a successful SQL connection.

## Reproduce checks and export evidence

`KIWIT_TEST_DATABASE_URL` must point to an isolated PostgreSQL instance. Run
`scripts/setup_local_tests.sh` (Python >=3.12) to install the pinned dependencies
and execute Python/database and Node tests. Keep temporary files/cache on a healthy
filesystem; on this Mac the external drive's previous TMPDIR was unusable.

`export_options_evidence.py --output /protected/new-bundle.json` runs a repeatable-read,
read-only export with a content checksum. `replay_options_evidence.py bundle.json`
checks recorded entry plans against the same selector used by the running desk.
It reports old unbound calls explicitly and never invents historical AI choices or
option P&L. This is reproducibility/parity evidence, not an out-of-sample backtest.
