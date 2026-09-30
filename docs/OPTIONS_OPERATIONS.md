# Options desk operations and audit remediation

The active engine remains the deterministic Bank Nifty options paper desk. Generic
V2 ML/replay/RL modules are research components and are not silently enabled by this
release. Live broker orders remain disabled. No provider/model change is made.

## Runtime

- Broker readiness: from 09:00 through 09:19 IST, calls only Groww's read-only
  profile and cash-quote endpoints. Missing daily approval raises one operational
  notification; recovery after approval and quote retrieval sends confirmation.
  The daily record preserves the first failure, failed-check count, alert delivery,
  and recovery time. It cannot place or cancel an order.
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

The Bank Nifty readiness banner shows `Groww session approval required` while the
pre-market check is failing. It clears only after both authentication and a
read-only quote succeed. `KIWIT_GROWW_READINESS_SYMBOL` optionally changes the
cash symbol used for the check; it defaults to `NIFTYBEES`.

Automatic database wake-ups are limited by a local clock/calendar policy:
`/live` is checked every minute without querying PostgreSQL. `/ready` and options
diagnostics run every minute from 09:00 to 17:00 IST on regular sessions, and only
at minute 00 outside that window. Unknown calendar years retain weekday daytime
monitoring so an outdated calendar does not hide a problem; this does not authorize
trading. Scheduled cash/options workers skip closed windows before constructing
database or broker clients. Reports retain quarter-hour catch-up during the daytime
window and hourly catch-up outside it. Manual CLI invocations without `--scheduled`
still run immediately.

This permits idle suspension between hourly off-hours checks. Off-hours database
failure detection and report retries may take up to an hour; process liveness stays
minute-level. Active dashboard polling, external probes of `/ready`, or manual
database work can still keep Neon awake. External uptime checks should use `/live`
for process liveness and reserve `/ready` for intentional dependency checks.

Annual regular-session calendar: NSE F&O circular FAOP71777, 2026, with
January 15 closure from amendment FAOP72262
(https://nsearchives.nseindia.com/content/circulars/FAOP72262.pdf). Source:
https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf . Unknown years block
entry; add the verified next-year calendar before year-end. Special-session times
must be implemented and reviewed before trading those sessions. Review exchange
amendments; this bundled calendar does not fetch notices automatically.

## Active selector, sizing and exits

The live paper desk is selector `banknifty-selector-v5-cost-aware`, exit policy
`playbook_cost_aware_v5`, sizing `options-sizing-v3`, cost model
`groww-nse-equity-options-2026-04-01` and prompt `banknifty-prompt-v3`. Catalogue
formulas live in [BANKNIFTY_PLAYBOOKS.md](BANKNIFTY_PLAYBOOKS.md). These playbooks
are unvalidated paper experiments. Live broker orders stay disabled.

Each new paper entry stores `exit_deadline` at fill time plus that playbook's
maximum hold. The supervisor is the execution authority. On a fresh option quote
it closes, in order, for session halt, disable or the 15:15 IST flatten
(`session_stop`), a pending exit, premium stop, premium target, a fresh
post-entry underlying candle through plan invalidation, then `playbook_time_exit`
once `now` reaches `exit_deadline`. A stale or missing option quote does not
invent a price or a time exit. AI EXIT stays advisory and does not close the
position. Positions that have no `exit_deadline` keep stop, target, halt and
end-of-day handling only.

Live holds, versioned stops and net reward multiples:

| Playbook | Maximum hold | Stop | Target floor | Net reward |
| --- | --- | --- | --- | --- |
| opening_range_breakout_v5 | 30 minutes | 4% | 8% | 1.5R |
| breakout_retest_v5 | 45 minutes | 4% | 8% | 2R |
| trend_pullback_v5 | 30 minutes | 3.5% | 7% | 1.5R |
| range_reversal_v5 | 20 minutes | 3% | 6% | 1.25R |
| previous_day_breakout_v5 | 45 minutes | 4% | 8% | 2R |
| engulfing_reversal_v5 | 20 minutes | 3% | 6% | 1.25R |
| hammer_reversal_v5 | 20 minutes | 3% | 6% | 1.5R |
| shooting_star_reversal_v5 | 20 minutes | 3% | 6% | 1.5R |

The session trade stop and target can only tighten those percentages. Stops and
targets are taken from the actual simulated fill. A plan is rejected when
estimated round-trip costs exceed 35% of planned gross reward. Sizing allows at
most 25% premium allocation and 1% of initial capital as planned risk, then the
remaining daily loss budget, and it buys whole lots only. These are hypotheses,
not guaranteed loss caps. `exit_experiments` horizons are counterfactual
evaluation windows. They are not the live deadline.

The experiment fingerprint covers provider, model, prompt, schema, sizing,
selector, exit policy, cost model and the session risk settings. The release SHA
stays on every record and is excluded from that fingerprint, so a compatible
release does not split the evidence series. Learning uses closed, same-day,
non-recovery trades from that exact experiment. Legacy unbound records stay
visible and are excluded from the current experiment's learning statistics.

## Historical experiment policy

The 19 September 2026 operations text described selector v3 and playbooks v3
with no fixed holding deadline. Premium, underlying invalidation, operator, halt
and end-of-day exits were the live policy for that series. Code commit `28afd30`
on 24 September 2026 replaced it with the cost-aware v5 deadlines above. Closed
v3 trades remain historical evidence under their own experiment id. Follow the
active section for current supervision, recovery and exits.

## Evidence and recovery

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
eligible call can probe recovery. Calls left reserved or completed by a process
interruption are marked `interrupted` after ten minutes, retain their conservative
charge and are never replayed. Independent exits continue throughout.

## Deployment and permissions

Provision `KIWIT_OPTIONS_EVENT_CALENDAR` before deployment. The referenced v2 JSON document must
cover the current IST day, be no more than seven days old, cite an official HTTPS source at the
calendar and event level, and use only `low`, `medium` or `high` impact. Run
`scripts/validate_options_event_calendar.py --path <calendar.json>` as the `kiwit` user before an
atomic replacement. Deployment performs the same validation and emits an explicit warning when it
fails; the release continues so observation and safety fixes are not stranded, while the runtime
calendar gate keeps entries fail-closed before any AI reservation. Do not use an empty event list
unless the cited source verifies complete coverage.

Apply migrations through 018 using the configured owner. 016 adds tracked-contract,
worker-incident and broker-cost tables. 017 adds the daily broker-readiness row.
018 adds the incident lifecycle columns and the one-active-incident index; the
worker incident section below requires it. The current accepted setup
uses `neondb_owner`; role separation is optional and is not a blocker for this build.
For a later role separation, provision roles using
`scripts/provision_database_roles.py --output /protected/path/roles.env` with
`KIWIT_MIGRATION_DATABASE_URL` supplied securely. It creates `kiwit_runtime` and
`kiwit_reader`, writes generated URLs to an exclusive mode-0600 file, and fails if
PUBLIC schema CREATE privileges would defeat the restriction. It never prints URLs.
For that optional setup, put only the runtime URL in `/etc/kiwit/kiwit.env`. Keep the migration URL in a
root-only `/etc/kiwit/migration.env`; services never source that file. The deployment
script refreshes runtime grants after migrations when the owner URL is configured.
Validate login, session writes, halts, workers, migrations and rollback before release.

`.github/workflows/ci.yml`, `security.yml` and `deploy-ec2.yml` are the release
path. CI and security install the locked runtime with
`scripts/runtime_dependencies.py`, and deploy compares the tested and scanned
manifests before calling `deploy/remote_deploy.sh`. The September 2026
`docs/audits/*workflow-update.patch` files are historical diffs. Do not apply
them. `latest-dependencies.yml` is a separate manual compatibility check and is
not the production install.

`remote_deploy.sh` installs that locked runtime before it drains workers. It
then runs `scripts/quiesce_deployment.py`. An open paper position, a non-flat
cash or intraday exposure, or a worker that does not drain defers the rollout
with exit 75 and restores the timers it stopped. Migrations have not started in
that case. A missing or invalid options calendar warns and leaves entries
fail-closed; it does not abort the release. Pending SQL then runs with
`KIWIT_MIGRATION_DATABASE_URL` when that file exists, otherwise with the runtime
URL. Grant refresh runs only when the migration URL is set. The supervisor
timer starts before the API restarts. Readiness failure after activation
quiesces again. If a position is open, rollback is deferred and the new release
stays up for an operator. Otherwise the script restores the previous release
directory, environment, units and nginx, then restarts them.

Migrations are forward only. Restoring the previous application leaves applied
SQL in place. After 018, `first_seen_at` and `last_seen_at` are required and
have no default, so a pre-018 build cannot insert `banknifty_worker_incidents`.
Tables added in 016 and 017 are unused by older code. No historical performance
or database role is silently modified by an application import.

## Release evidence

Checked 30 September 2026 against this runbook's source tree:

- `origin/main` was `a75a01379e156afe43cc78067683bf74c284de76` (merge of KIW-52).
- [Deploy EC2 run 36722113204](https://github.com/imsubhamm/kiwiT/actions/runs/36722113204)
  completed successfully for that SHA.
- Public [https://kiwit.tathyaforge.in/health](https://kiwit.tathyaforge.in/health)
  returned `execution: paper-only` and the same release SHA.

That establishes merged and deployed for `a75a013`. It does not establish
accepted strategy evidence. The 28 September 2026 audit of release `251ea21`
observed `schema_migrations` version 018; this tree still ends at migration 018,
and this documentation change did not re-query production `schema_migrations`.
A SHA-bound observation, plan, paper entry, independent exit, reconciliation
and report chain with replay parity is still outstanding. Record that chain with
[OPTIONS_ACCEPTANCE_RUNBOOK.md](OPTIONS_ACCEPTANCE_RUNBOOK.md) before treating
the deployed SHA as accepted. Missing external evidence stays incomplete.

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
checks full recorded entry plans against the same selector used by the running desk,
then checks entry authority, fill prices, sizing, stops/targets and partial-exit accounting.
It cannot reconstruct external halt/consent history or authenticate an operator's settlement source.
It reports old unbound calls explicitly and never invents historical AI choices or
option P&L. This is reproducibility/parity evidence, not an out-of-sample backtest.

## Groww IV and Greeks coverage (KIW-44)

Groww's official live quote response documents implied volatility, open interest,
and volume. Its official option-chain response additionally documents provider
values for delta, gamma, theta, vega, rho, and IV for each CE and PE contract.
KiwiT therefore fetches the option chain once for the selected expiry and joins
the returned fields to executable per-contract quotes by trading symbol. It never
derives, estimates, or fills a missing volatility value.

Each accepted value records its provider, endpoint, observation time, and timestamp
basis. The option-chain schema does not document an exchange timestamp, so KiwiT
records the client receipt time explicitly and rejects data more than 30 seconds
old or future-dated. It rejects non-finite values, IV outside `(0, 500]`, delta
outside `[-1, 1]`, negative gamma or vega, and negative or fractional volume and
open interest. Theta may be positive or negative but must be finite.

`option_feature_coverage` reports `full`, `partial`, or `unavailable` for every
field, including the field's observed sources and latest observation time. The
complete coverage object remains in `banknifty_market_history` and appears as a
top-level section in the reproducible options evidence export.

Current live playbooks use prices and liquidity and declare no required volatility
fields. A future volatility-dependent playbook must declare `required_option_fields`.
The selector then skips any contract missing one of those fields with
`OPTION_FEATURES_UNAVAILABLE`; price-only rules remain eligible. This is the
explicit fallback when the provider endpoint is unavailable or coverage is partial.

Provider references:

- [Groww live data API](https://groww.in/trade-api/docs/curl/live-data)
- [Groww API changelog](https://groww.in/trade-api/docs/curl/changelog)

## Worker incident lifecycles (KIW-45)

This lifecycle requires migration 018. Worker incidents use the worker name and a normalized reason code as their active
identity. Repeated identical failures update one lifecycle's last-seen time,
occurrence count, and latest evidence while preserving its first-seen time and
initial detail. A changed reason closes the previous lifecycle as
`reason_changed` and starts a separate incident. Recovery closes the active
lifecycle once, records its recovery time and evidence, and repeated recovery
signals are ignored. If the same failure later recurs, it creates a new lifecycle.

The migration preserves legacy rows as closed historical evidence. New active
lifecycles are unique per worker, preventing concurrent pollers from creating
duplicate incidents. Operations diagnostics and evidence exports expose the full
lifecycle fields for audit and alerting.
