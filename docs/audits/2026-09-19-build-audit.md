# NitiQuant build audit — 19 September 2026

## Verdict and scope

The build is an experimental paper platform with useful deterministic execution controls. Its principal gaps are reliable AI operation, recovery accounting, reproducible strategy evidence, and integration of the research components into the running desk. More strategies or a more elaborate UI would not resolve those gaps.

Audited recovered checkout `/Users/imsub/kiwit-recovered` at `2d90d2db70daefc10accd79ef49492159ff7845c`, matching the production release. The configured `/Volumes/Flash/TradingKIWI` directory currently exposes no files. Inspected code, documentation, tests, CI results, systemd service states and read-only production database summaries. No production settings, sessions, trades, emails or deployments were changed. This is not a penetration test or profitability assessment; visual/browser QA and external infrastructure backup verification were not performed.

## Prioritized findings

### 1. Medium — provider fix deployed; structured failure diagnostics remain incomplete

Correction after reviewing the earlier task: the user already addressed the TokenHarbor/DeepSeek timeout problem by switching production to OpenAI `gpt-5.6-terra` on September 18. That task records a successful completed API probe in 1.67 seconds. This audit independently confirmed the current OpenAI/Terra configuration. The historical September 17–18 failures do not establish that the new provider is failing. Successful execution of a full eligible decision after the switch has not been established by this audit.

Historical production records: September 17 has four failed AI calls, September 18 six failed calls; neither day has an entry. September 15 has eight failed calls and five applied decisions. The API, Bank Nifty timer and watchdog timer are active. The original audit incorrectly used historical failures to describe the current provider as an immediate blocker.

`src/kiwit/banknifty.py:770–776` replaces timeouts, HTTP errors, JSON/validation failures and local request-size errors with the same generic failure. `settle()` stores no typed failure reason. Therefore the audit cannot determine whether recent failures came from credentials, quota, provider behavior, response truncation or another cause. The three most recent failed snapshots serialize below the current request-size limit; oversized requests are not demonstrated as their cause.

Action: retain sanitized failure category, HTTP status, provider request ID where available, latency, model and response completion status. Never store credentials or unrestricted provider bodies. Basic AI-failure email alerts were already added in commit `3ae89d1`; extend these with durable delivery status and sustained-failure handling where needed. Verify a complete eligible decision with the configured provider. An empty trading day must distinguish no eligible setup from AI unavailability.

### 2. High — prior-day residual positions contaminate ordinary daily performance

Production contains a September 1 session with one entry and realized P&L `7107.28400`. Its exit quote timestamp is September 15 at `03:59:54+00:00`; the exit reason is `session_stop`. Thus this is not an ordinary September 1 intraday result. It also has no daily report.

`banknifty.py:_monitor` deliberately moves an old session into stopping and `_close` records the fill against that old session. This preserves a real quote rather than fabricating a close, but daily summaries and learning aggregates do not separately classify recovery trades or expose overnight holding risk.

Action: preserve the original ledger, add actual entry/exit dates and holding duration to performance exports, classify overdue/recovery trades, exclude them from ordinary intraday comparisons by default, and define settlement/reconciliation for a contract that expires while unresolved. Add an operator recovery workflow and alerts; do not manufacture a closing price.

### 3. High — runtime database credentials own the trading tables

Read-only production checks show the environment database user is `neondb_owner`; it owns `banknifty_sessions`, `banknifty_events`, and `schema_migrations`. This contradicts `docs/DATABASE.md`, which requires separate owner/runtime/read-only roles. A runtime compromise has a much wider database modification boundary than necessary.

Action: use a separate migration owner and least-privilege runtime role; use a read-only role for diagnostics. Validate permissions and deployment compatibility before switching credentials. Ownership was verified; no destructive permission tests were performed.

### 4. High — a valid holiday-shortened week makes every playbook ineligible

`chart_analysis.py:94–115` requires exactly five prior Monday–Friday sessions and hardcodes `calendar_verified=False`. `playbooks.py:73–77` rejects incomplete coverage. A local synthetic reproduction with four complete sessions returns incomplete even when the missing weekday represents a holiday. There is no holiday input in this function. Special sessions and shortened sessions need explicit treatment too.

Action: validate coverage against expected sessions from a versioned exchange calendar. Preserve missing-data rejection; distinguish a scheduled closure from absent data. This is a correctness problem, not a reason to weaken freshness controls.

### 5. Medium — strategy behavior changed without separating evidence versions

Commit `15ce7c8` intentionally removed holding deadlines; all four `max_hold_minutes` values are now `None`, and `test_elapsed_holding_deadline_does_not_force_exit` explicitly verifies that behavior. However, selector version remains `banknifty-selector-v1`, the playbook IDs remain v1, and `docs/BANKNIFTY_PLAYBOOKS.md` still promises 30/45-minute exits. Trade-stop/target policy also changed in later commits.

`banknifty.py:learning_context` groups outcomes by playbook ID. Consequently results from different exit policies can be combined under the same label. Per-call prompt/release bindings are also insufficient for exact reconstruction: the stored snapshot is not the complete versioned request configuration.

Action: retain the chosen holding policy, update its documentation, and bind every trade/decision to selector, exit-policy, sizing, prompt, model/provider and code-release versions. Split evidence across those versions instead of treating it as one experiment.

### 6. Medium — reports have neither durable catch-up nor concurrency-safe delivery claims

`banknifty.py:423–447` only processes the current date after 15:30 and stops after three attempts. Missing configuration consumes attempts. Production reports for September 15–17 are `not_configured` with three attempts; September 18 is sent. The missing September 1 report cannot be generated by this normal current-date path now.

Sending occurs after releasing the lock and before marking sent. Concurrent invocations, or a crash after SMTP accepts the email but before the database update, can duplicate delivery despite the documentation's idempotency claim. The single systemd unit reduces routine overlap but does not fix crash semantics.

Action: use a durable report/outbox backlog, claim records with a lease, retain delivery evidence, expose retry controls and define at-least-once delivery semantics. Do not consume a transport retry for missing configuration. Catch up missed dates explicitly.

### 7. Medium — the watchdog checks web/database readiness, not the Bank Nifty decision loop

`scripts/health_watchdog.sh` only calls `/ready` and writes to syslog. `operational_readiness.py` examines cash-worker freshness and reports V2 readiness as unassessed; it has no Bank Nifty dependency argument. A stopped Bank Nifty worker or repeated decision failure can coexist with a healthy web API.

Action: monitor expected-session heartbeat, successful market scan, consecutive AI failures, quote age, unresolved positions, budget exhaustion and report delivery. Alert through a verified channel and keep market-closed silence distinct from in-session failures.

### 8. Medium — deterministic local rejection consumes the same budget as an ambiguous network call

`banknifty.py:770–776` reserves before `analyst.decide`. `options_ai.py:request_body` can reject an oversized payload before HTTP is attempted; the caller still settles it as failed and retains $0.20. Repeated local invalid payloads can exhaust the $2 daily or $18 lifetime internal allowance without provider spending. Production has $5.928520 used/reserved and 27 failed calls; this is not a provider invoice and is not proof these failures were locally rejected.

Action: run deterministic validation/serialization before reserving; distinguish never-dispatched from potentially-billed attempts. Retain conservative reservations for ambiguous network outcomes. Add a reviewed budget renewal/reconciliation process instead of silently resetting the trial ledger.

### 9. Medium — historical candles are present, but options/AI validation is still missing

Production `banknifty_market_history` contains 46,500 rows across 124 dates, March 18–September 17. Every row is labelled `Historical market-data backfill`; none is a live scan row at audit time. This does not establish continuous coverage, historical executable option prices, or historical AI decisions. The latest storage release was deployed after the session, so absence of live rows is not itself evidence of a broken writer.

`run_once` returns before snapshot acquisition unless there is an enabled running session in the entry window (`banknifty.py:676–681`). The current design therefore does not collect a continuous market research tape while idle, after an early completed session, or after the entry cutoff.

Action: separate read-only observation from permission to enter paper trades. Build coverage/gap/provenance reports; retain point-in-time contracts and executable option quotes for prospective evaluation. Do not use underlying candles alone to claim option P&L. Evaluate each playbook and the AI selector out of sample against a deterministic selector and no-trade baseline with versioned costs.

### 10. Medium — research components are implemented but not one integrated deployed engine

The Bank Nifty service directly uses chart analysis, playbooks and `OpenAIPaperAnalyst`. It does not invoke the V2 specialist ML stack, V2 decision journal, unified backtest, or generic LLM critic flow. `session_adapter.py` explicitly supports only `legacy-cash-observer` and raises for other engines. Implementation of these modules does not mean the deployed options desk uses them.

Action: choose the intended production engine, document the call path, then complete end-to-end journal/outcome wiring and replay/paper parity. The September 13 status document lists pending real-data training/evaluation, paired LLM benchmarks, restart drills and RL integration; those historical statuses require fresh acceptance evidence before being closed. Keep RL work behind these prerequisites.

### 11. Medium — exit supervision shares the slow scan/model worker

`run_once` monitors once, then performs a full snapshot and possibly model inference. Snapshot acquisition downloads/parses the instrument master each scan and fetches candidate quotes sequentially. The systemd service is a oneshot with a 110-second timeout and a minute timer; an already-running service does not supply a second independent exit check at the next minute.

Action: measure worst-case tick latency and skipped supervision intervals. Cache the instrument master with explicit validity, bound scan work, and isolate position supervision if the required exit cadence cannot tolerate the worst-case scan/model time. Existing logical independence from model success does not provide scheduling independence.

### 12. Lower — reproducible local validation and deployment dependencies need work

Full local pytest stops at collection with 11 errors from missing numpy, pydantic, FastAPI and LangGraph, with six skips reported. Local Python is 3.14 while CI uses 3.12. Focused chart/playbook/Bank Nifty tests: 81 passed, 25 skipped (database-dependent coverage unavailable locally). Dashboard Node tests: 23 passed. Broad local Ruff run reports 65 findings under the effective local configuration; many are style issues, not behavioral defects.

GitHub CI, Security and Deploy EC2 all report success for the audited SHA. Those results should not be confused with the failing local setup. `pyproject.toml` mostly specifies ranges and deployment resolves packages afresh, so the tested and deployed dependency graph is not guaranteed identical.

Action: provide a reproducible Python/test environment and locked deployment dependencies, run integration tests against an isolated PostgreSQL instance, and standardize lint configuration/scope. Do not run destructive test suites against the production database. Confirm actual backup/PITR and restore-drill evidence separately: requirements exist in docs, but this audit did not verify the external backup service.

## Rules that have become boundaries

| Rule | Current effect | Audit disposition |
| --- | --- | --- |
| Prior week must contain five weekday sessions | Scheduled holidays can disable all entries | Correct the calendar model |
| Weekly direction veto plus 5m/15m agreement | Intraday reversals can be rejected despite a fresh setup | Measure veto rate and counterfactual outcomes; do not assume removal improves results |
| Four fixed playbooks; AI can choose only supplied plans | AI is a bounded selector, not a strategy inventor | Make this capability clear; expand only through versioned experiments |
| Three nearest strikes, nearest eligible expiry, long options only | Limits available exposures and alternative structures | Deliberate scope; needs evidence before expansion |
| Five-minute AI slot, 90-second plan lifetime, 0.5% premium allowance | Delay or fast movement can make valid plans unfillable | Measure rejection/latency distributions before tuning |
| Whole lots, 25% allocation, 1% planned risk, fees | Small capital can produce zero affordable quantity | Show minimum feasible capital and exact sizing blockers |
| Immutable same-day limits; one zero-entry resume | Mistakes can make a day unusable, but stop risk-limit resets | Improve pre-run preview; preserve loss-accounting continuity in any edit flow |
| Manual RUN gates both observation and entries | Idle/finished days do not produce a continuous tape | Separate collection from execution consent |
| Lifetime $18 application budget and retained failed reservations | Trial eventually stops new AI decisions | Expose remaining allowance and audited renewal/reconciliation |
| Freshness, whole lots, halt, no live mutations | Reduces unsafe or invented execution | Keep these boundaries |

## Additional requirements and overlooked cases

- Treat stops as triggers, not guaranteed loss caps: stale quotes, minute polling, gaps and partial fills can exceed planned risk. The current fee model is explicitly illustrative.
- Add an eligibility funnel: observed scans → usable data → pattern → regime/weekly checks → affordable plan → AI success → accepted fill. Count each rejection reason. Zero trades alone does not identify the restricting rule.
- Learning based only on selected/closed trades is selection-biased. Preserve rejected plans and evaluate counterfactual outcomes under the same executable-data assumptions. Twenty trades being labelled exploratory does not establish an edge.
- Test lost SMTP acknowledgements, host downtime at close, incomplete option chains, expired unresolved contracts, deployment during an open position, restart while an AI reservation exists, holiday weeks and partial exit liquidity.
- Reconcile product documentation, UI status, actual enabled runtime and issue acceptance. Avoid reporting a component as operational solely because code/tests exist.
- Record and rehearse backups, restore, halt races, release rollback and schema compatibility. Application rollback after a forward migration is not automatically database rollback.

## Recommended work order

1. Verify full eligible decisions after the completed provider switch; add Bank Nifty health and actionable failure categories.
2. Reconcile the overdue September 1 trade in performance reporting; implement overdue-position and missed-report recovery.
3. Separate database privileges and verify backup/restore evidence.
4. Correct holiday coverage and version the actual strategy/exit/prompt configuration.
5. Collect a continuous versioned research tape and an eligibility funnel.
6. Complete one integrated paper/replay path, then evaluate strategy and AI contribution on real held-out evidence.
7. Tune restrictive rules from those results; defer strategy expansion and RL until the operational/evidence gaps close.
