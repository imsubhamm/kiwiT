# Cursor implementation brief: NitiQuant build audit

Date: 21 September 2026
Audited source/deployment: `main` / `1a14f0046abf7f3bc0503a54e7d3cf2b08a1e3e6`
System status: deployed, paper-only, database connected, schema version 15

This document is written as an implementation brief for Cursor. Treat the findings
and acceptance criteria as the scope. Preserve unrelated local files and changes.
Do not enable live broker mutations, invent market prices, force a trade, weaken a
halt, or claim profitability from unit tests.

## Current verified state

- GitHub CI, Security and Deploy EC2 passed for the deployed release.
- API, watchdog, decision, observer, supervisor and reports timers are active.
- The reduced off-hours database polling change from PR #8 is deployed. Process
  liveness stays minute-level; PostgreSQL checks are frequent during the market
  window and hourly outside it.
- At approximately 09:51 IST today, the current paper session was running with zero
  entries. Observer, supervisor and decision heartbeats were healthy.
- Today has 28 live observations and 22 strategy scans. One AI `BUY` result completed,
  consumed approximately $0.03157 in the internal ledger, and was rejected by the
  independent execution recheck because the option spread, freshness or maximum
  entry price condition failed. This is correct fail-closed behavior, but the combined
  reason is too coarse for diagnosis.
- Six closed historical outcomes exist, one marked recovery. This sample does not
  validate any strategy or the broader selector.
- Existing daily reports are sent. A missing-report failure path can still be invisible.
- A local logical backup/restore previously matched all 43 user tables. Managed Neon
  PITR and a production-data restore remain unverified.

## Priority 0: defects to fix next

### P0.1 Dependency outages cannot use the built-in notifier

Problem: `scripts/health_watchdog.sh` exits when `/live` or `/ready` fails. The
database-backed `scripts/banknifty_health.py` notifier is not reached after readiness
failure and could not claim a notification while PostgreSQL is unavailable anyway.
`deploy/kiwit-watchdog.service` has no independent `OnFailure` notification path.
The host journal is evidence, not an external alert.

Required change:

1. Add a dependency-independent alert path for API/database failure. It must not need
   the application database, API process, or the failed service itself.
2. Deduplicate and rate-limit notifications using host-local state under an explicitly
   writable protected directory, or use an external uptime/alerting service.
3. Preserve minute-level `/live` checks and the new database wake-up schedule.
4. Do not include curl response bodies, credentials or database URLs in notifications.
5. Add a recovery notification after a sustained failure clears.

Acceptance:

- Two consecutive `/live` or scheduled `/ready` failures produce one external alert.
- Repeated failures do not send more than the configured rate.
- Recovery produces one recovery message.
- The behavior works with PostgreSQL stopped and with the API stopped.
- Unit tests use a fake notifier; tests never send a real message.

### P0.2 Missing daily reports are not detected

Problem: `src/kiwit/options_operations.py::diagnostics` counts report rows whose
delivery status is not `sent`, but does not count due sessions that have no report
row. It also does not require a reports-worker heartbeat. A completed session after
15:30 IST can be missing its report while diagnostics returns `ok` and
`pending_reports=0`. This failure has been reproduced in an isolated database.

Required change:

1. Count due sessions with no report separately from unsent report rows.
2. Publish a reports-worker heartbeat on every scheduled invocation, including
   `not_configured`, no-work and failure outcomes.
3. Degrade operations for an overdue missing report or stale reports heartbeat.
4. Keep report generation idempotent and delivery at-least-once. Do not claim
   exactly-once email delivery.
5. Show missing, generated-unsent and retrying counts separately in the API/dashboard.

Acceptance:

- A completed or due session with no report after 15:30 IST is unhealthy.
- A generated but unsent report is distinguishable from a missing report.
- An empty healthy backlog remains healthy.
- A failing old delivery cannot starve a newer report.
- The reports worker still performs hourly off-hours catch-up without increasing
  automatic database wake-ups.

### P0.3 Replay returns success for failed fill parity

Problem: `scripts/replay_options_evidence.py` exits nonzero only when the selector
plan mismatch count is nonzero. `src/kiwit/options_replay.py` reports entry/exit fill
mismatches in `fills`, but does not include them in the process result. A tampered
entry price of `999` produces `fills[].status=mismatch` and exit code 0.

Required change:

1. Add explicit totals for selector matches/mismatches/unreplayable calls and fill
   matches/mismatches/unreplayable events.
2. Make the CLI exit nonzero for any mismatch.
3. Add `--strict-evidence` so missing/legacy-unreplayable records also fail a release
   gate. Make output clearly state whether strict mode was used.
4. Preserve the distinction between mismatch and unavailable evidence.

Acceptance:

- Tampered plan, entry price, quantity, stop, target, partial exit, fee or settlement
  accounting each cause a nonzero exit.
- Missing legacy evidence is reported as unreplayable, never as a match.
- Strict mode fails for unreplayable evidence; informational mode may report it.

### P0.4 Incomplete deployment acceptance returns success

Problem: `scripts/evaluate_options_evidence.py --day ... --release ...` prints
`session_acceptance.status=incomplete` but exits 0. An empty bundle reproduces this.
Automation can therefore accept an incomplete post-deployment session.

Required change:

1. Add `--require-complete-session`; when present, return nonzero unless every
   acceptance check is true.
2. Return a distinct nonzero code for malformed evidence versus valid-but-incomplete
   evidence.
3. Keep ordinary comparison reports informational unless a strict gate is requested.
4. Document that a quiet/no-setup day is valid operation but cannot prove the full
   entry/exit chain. Never force a trade to satisfy the gate.

Acceptance:

- Empty or partial session evidence fails strict mode.
- A complete observation → decision → entry → closed exit → flat report → sent
  delivery chain passes only when all evidence belongs to the requested release/day.
- Informational evaluation remains usable for incomplete datasets.

## Priority 1: required operational completion

### P1.1 Complete and archive a real deployed market-session acceptance run

Today proves observation, strategy scans, an AI response and fail-closed entry
validation. It does not prove a successful paper entry, partial/full exit, settlement,
or same-release report delivery.

Build a read-only acceptance exporter that records release SHA, schema/calendar
version, worker timestamps, observation coverage, AI result, validation outcome,
position lifecycle, reconciliation and report delivery. It must never trigger a trade.
Run it after a naturally occurring eligible trade. Store the evidence bundle hash and
the strict acceptance result outside the production database as a release artifact.

### P1.2 Make execution rejection reasons precise

`src/kiwit/playbooks.py::validate_plan` currently combines option spread, quote age
and maximum entry price into one error. Split these into stable reason codes with safe
numeric evidence: quote age, observed spread percentage, refreshed fill, and plan
maximum fill. Do not store provider bodies or secrets. Add funnel counts to the
dashboard so the operator can distinguish an intentionally narrow rule from broken data.

### P1.3 Finish managed backup/PITR verification

Confirm Neon PITR retention in the provider control plane, restore a selected
production timestamp into an isolated branch/database, verify migration history,
roles/grants, table manifests and application read-only health, then record RPO/RTO and
cleanup ownership. Do not restore over production. The existing logical restore tool
does not prove managed PITR or role recovery.

### P1.4 Apply the CI lock and full-lint workflow changes

The deployed repository workflows still install application dependencies without
`requirements.lock` and lint selected source paths rather than all `src` and `tests`.
The intended changes remain local and in
`docs/audits/2026-09-20-workflow-update.patch` because an earlier credential lacked
workflow scope.

Apply the patch using a workflow-authorized credential. Verify CI, Security and Deploy
EC2 all pass. Do not weaken `pip-audit`, Bandit or the private-key scan.

### P1.5 Assign operational ownership and external limits

Record named owners and review dates for:

- NSE F&O calendar updates and special sessions;
- Neon compute/storage quota alerts and billing review;
- raw tape growth, retention and off-host archive restore verification;
- EC2 disk, logs, certificates and deployment credentials;
- quarterly restore drills and post-deployment acceptance evidence.

The current fallback text “deployment operator owns it” is not durable ownership.
Configure provider-side alerts before quota exhaustion rather than relying on an
application connection failure.

## Priority 2: evidence missing before strategy conclusions

### P2.1 Real options and execution-cost validation

The cost model in `src/kiwit/options_risk.py` is illustrative: a flat 0.2% fee plus
₹20 and a fixed 0.1% adverse fill adjustment. It is not reconciled to actual broker
contract notes, exchange charges, taxes, spread behavior, latency or partial fills.

Create a versioned broker-cost model backed by actual paper/contract-note evidence.
Report every cost component and compare modelled versus observed costs. Run cost stress
without overwriting raw evidence.

### P2.2 Retain evaluation contracts after they leave the five-strike universe

`src/kiwit/options_market.py::snapshot` records only contracts at the five strikes
nearest the current spot. A contract selected at decision time can leave this moving
window before the evaluation horizon, causing its future quote to disappear and the
counterfactual to be excluded. This can create coverage related to subsequent spot
movement.

Persist a bounded tracked-contract set for open positions and evaluation plans until
their horizon/expiry ends. Measure inclusion/exclusion rates by playbook, direction,
time and volatility before interpreting AI-versus-baseline comparisons.

### P2.3 Run chronological AI-versus-baseline comparisons

Use frozen forward quote tapes and compare:

- AI selection versus HOLD/no-trade;
- AI selection versus first eligible deterministic plan;
- each playbook versus the others;
- current rules versus proposed boundary changes;
- normal costs and stressed costs.

Prevent look-ahead, group by stable experiment identity, report missing-data
exclusions, use non-overlapping portfolio simulation for capital returns, and reserve
an untouched final period. Fixed-horizon opportunity P&L is not a portfolio backtest.

### P2.4 Integrate ML/V2/RL only through explicit adapters and promotion gates

The ML registry, V2 engine and RL shadow environment are separate research systems.
They are not connected to the deployed Bank Nifty options desk, and no trained
options-compatible policy has passed held-out evaluation. Do not wire them directly
into production decisions.

First define versioned, read-only adapters from the same point-in-time option tape;
prove replay/paper parity; bind artifacts and feature schemas; include the existing
risk/settlement lifecycle; run champion/challenger comparisons; require manual
promotion. Keep every output advisory until those gates pass.

## Things that are imperfect but intentionally bounded

- Email delivery is at-least-once. A crash after SMTP acceptance can duplicate a
  report. Keep the claim lease and state this limitation unless the provider supplies
  an idempotency key or durable receipt lookup.
- Interrupted ambiguous AI calls retain their reservation. This is conservative but
  can overstate usage. Reconcile against provider usage without silently refunding.
- `neondb_owner` remains explicitly accepted for now. It is not a blocker for this
  task. Reconcile `docs/DATABASE.md`, which still says the runtime must never use the
  owner, with this temporary accepted exception and its review trigger.
- `/ready` confirms service dependencies, not strategy eligibility or profitability.
- Public `/live` and `/health` are process endpoints; avoid putting operational or
  secret data in them.
- Historical backfill and current underlying candles do not substitute for historical
  executable option bid/ask/depth.

## Rules that form boundaries

Classify rules before changing them. Do not remove all boundaries in one selector
version because the result cannot be attributed.

### Keep as safety/integrity boundaries

- paper-only execution and disabled live broker mutations;
- no fabricated fills or settlement prices;
- fresh, timestamped executable quotes and completed candles;
- whole lots, positive displayed depth, cash and freeze-quantity limits;
- immutable plan IDs, execution recheck and database locking;
- global/operator halt, one open position and independent exits;
- verified exchange calendar, expiry handling and explicit reconciliation;
- bounded AI schema, budget accounting and no model-controlled quantity/risk limits.

These rules protect accounting or execution integrity. Evaluate their implementation,
but do not weaken them merely to create more trades.

### Experimental boundaries that require comparison

- complete previous-week coverage as a hard entry prerequisite;
- either 5m or 15m regime support;
- eight fixed playbooks;
- nearest non-expiry-day expiry and five nearest strikes;
- 2% maximum spread;
- 90-second option quote, 180-second chart and 300-second pattern freshness;
- two-minute AI slots and 20 KB request cap;
- ten entries per day, one position and five-minute post-exit cooldown;
- entry cutoff at 15:00 and flattening from 15:15;
- 50% premium allocation and 2% planned-risk cap;
- $5 daily and $50 rolling-30-day AI allowance;
- no fixed maximum holding time before session exit.

For each proposed change, create a new selector/risk version, state the hypothesis,
run the same frozen period under current and proposed rules, compare opportunity count,
fill coverage, costs, drawdown, unresolved exits and net portfolio results, then collect
forward paper evidence. Change one coherent rule family at a time.

## Recommended implementation order for Cursor

1. Fix P0.2, P0.3 and P0.4 with database and CLI tests.
2. Design P0.1 using the project's actual external notification service; keep tests
   local and do not send real alerts during development.
3. Add precise entry-rejection reason codes and dashboard funnel visibility.
4. Apply the workflow lock/full-lint patch with the required GitHub permission.
5. Build the acceptance exporter and run it without triggering trades.
6. Complete provider PITR and ownership work with operator evidence.
7. Extend option-tape retention and cost modelling before strategy comparisons.
8. Evaluate rule changes and only then consider ML/V2/RL integration.

## Definition of done

The build is operationally complete for its current paper-only scope only when:

- runtime/process/database failures notify through a verified independent path;
- missing reports and stopped workers cannot appear healthy;
- replay and acceptance commands fail closed in strict mode;
- a natural same-release market session proves observation through delivered report;
- managed PITR and isolated restore evidence meet documented RPO/RTO;
- CI uses the dependency lock and covers all source/tests;
- real options/cost data supports reproducible held-out comparisons;
- calendar, quota, retention and restore ownership are named and exercised;
- strategy boundaries are changed through versioned evidence, not intuition;
- ML/V2/RL remain research-only until their explicit promotion gates pass.

Passing unit tests alone is not this definition of done. No live-trading readiness or
profitability claim should be made from the current evidence.
