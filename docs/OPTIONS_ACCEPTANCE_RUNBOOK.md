# Options paper-desk acceptance runbook

This runbook verifies the deployed paper desk without creating a live order or forcing
an AI trade. It is required before treating the broader selector rules or any research
module as empirically validated.

## First regular-session verification

On a verified regular NSE session, confirm the deployed release and all four timers:
observer, supervisor, decision and reports. Between 09:30 and 15:00 IST, verify the
dashboard shows fresh observer and decision heartbeats plus recorded
`live_observation` rows. Start one paper session only with the intended limits. A
valid setup may never appear; accept `waiting_for_setup` as the correct outcome when
the eligibility funnel says so. Do not change thresholds or force an order to make
this check pass.

If a qualified plan appears, verify the chain in the database and dashboard:
observation -> strategy_scan -> bounded AI call -> applied/rejected decision ->
paper entry only after independent quote/underlying recheck. At close, verify
supervisor execution, daily report generation and report delivery status. For an open
position, exercise STOP only in paper mode and verify an executable quote is used.
Record the release SHA, calendar version, worker timestamps, decision result and
report delivery status in the deployment log.

## Backup and restore drill

Confirm the provider's PITR retention setting and timestamp in its control plane.
Restore a chosen timestamp into a new isolated database, never the production
database. Connect with a read-only role and check schema version, session/event/call
counts, `banknifty_trade_outcomes`, daily reports and a content checksum from
`scripts/export_options_evidence.py`. Run tests against a separate disposable test
database; preserve the restored database for comparison. Record restore duration,
selected timestamp, checksum and the operator responsible for cleanup.

For a logical backup drill, set `KIWIT_BACKUP_SOURCE_URL` and
`KIWIT_RESTORE_TARGET_URL` through the existing secret-management environment, then
run `python scripts/restore_drill.py --output /private/new-drill-directory --pg-bin /path/to/postgres/bin`.
The target must be an empty isolated database. The tool uses a consistent snapshot,
restores transactionally, and compares every user table's count and row-content hash.
It leaves the private dump and result for inspection and never drops data.
This verifies logical restoration, **not managed PITR** or recovery of provider roles.

## Evidence and research gates

Export a frozen evidence bundle each completed week. Track market-tape coverage,
missing intervals, option bid/ask/depth, plan eligibility, AI latency, rejection
reason, paper fill, partial exit, and final settlement. Evaluate each selector version
against HOLD/no-trade and deterministic-plan baselines on untouched forward periods.
Use actual observed option quotes and costs; underlying-only history cannot validate
option profitability.

Run `python scripts/evaluate_options_evidence.py --help` for paired fixed-horizon
AI versus first-eligible-plan/HOLD comparisons and deployment-session checks.
Quotes before response completion are excluded; missing future prices remain missing.
Report paired sample counts and exclusions. This is an opportunity study, not a
portfolio backtest or realistic broker-cost validation.

ML, V2 and RL modules remain research until their own frozen datasets, chronological
out-of-sample comparisons, paper/replay parity and manual promotion evidence exist.
No runbook step enables live execution.

## Operations ownership

The deployment operator owns calendar maintenance and monthly storage review until
a named owner is recorded in the deployment log. Before each year-end, that operator adds the next NSE F&O calendar and any
exchange amendment, then run the calendar tests. Review tape size monthly and choose
an approved retention/archive window after measuring database size and evaluation
needs. Keep the raw evidence needed for any published experiment manifest. Default:
retain all raw tape; export monthly to access-controlled storage, retain its SHA-256
and verify restoration before approving any deletion. Never delete tape referenced
by an experiment or unresolved position. Set a storage alert at 80% of the provisioned
database capacity in the provider console; record capacity and measured monthly growth.
