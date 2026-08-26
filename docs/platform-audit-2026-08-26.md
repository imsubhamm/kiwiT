# kiwiT platform audit — 26 August 2026

## Verdict

Functional research/paper prototype, not a production-ready trading platform. Live Groww mutations remain hard-disabled. A working quote feed does not prove strategy approval, reliable execution, or profitable performance.

Scope: source-level review of dashboard, APIs, intraday worker, paper ledger, research status and deployment; local automated tests; read-only EC2 service/health and tunnel journal checks. This is not a full security review or a completed live-broker failure-drill campaign.

## Availability incident

The existing Quick Tunnel process stopped/restarted at 06:17:47 UTC (11:47:47 IST). The new process registered `souls-inquiry-detected-unified.trycloudflare.com`. The old hostname returned error 1033. The initiating actor/cause of that restart was not established. During this audit the API, nginx and tunnel were active; the replacement public health endpoint and login returned successfully. No tunnel restart was performed by this audit.

Quick Tunnels are temporary, not reservable production hostnames. A named tunnel on an existing controlled domain is the preferred stable-address path; changing DNS requires the owner's approval. The deployment only reloads nginx/restarts the API, not the tunnel. Hard-coded dashboard/email URLs must be changed if the Quick Tunnel changes again.

## Interaction fixes in this change

| Control | Finding | Resolution |
|---|---|---|
| Sidebar | Hidden below 760px; labels hidden at intermediate widths; observer could highlight the wrong section | Mobile horizontal navigation, readable labels, explicit scroll/focus and selected item on click |
| Sync | No polling, no timeout; one failing API prevented all successful responses rendering | 30-second visible-tab polling, 15-second timeout, deduplication, independent panel results, partial-sync feedback |
| Errors | Error text at bottom of a long page | Sticky action/error feedback near controls |
| Feed | Displayed age froze between syncs | Recomputed quote age and visible quote/worker timestamps; unavailable signals remove stale action cards |
| Approve/reject | Only pending signals expose controls; empty state did not explain it | Explicit pending requirement, duplicate-click guard, mutation feedback; no invented production signals |
| Halt | Dashboard wrote `system_halts`, but intraday approval ignored that table | Intraday approval checks account/global halt under a share lock before any fill; exits remain monitored |
| Account selector | Portfolio accepted a typed account, signal desk used server-configured account | Read-only single-account selector until properly scoped multi-account support exists |
| Research | Metrics duplicated as static HTML; API snapshot not fetched | Clearly label archived cards, fetch server snapshot status during sync; do not claim a research rerun |
| Email/reconciliation | API returned records but UI omitted them | Display delivery attempt counts and latest reconciliation summary |
| Decorative metrics | Sparklines were decorative, not computed performance | Hide misleading curves |
| Local file | `file://` cannot load root-relative assets/authenticated APIs | Visible notice directing operators to the hosted page |

## Remaining blockers (not fixed by this UI patch)

### P0 — required before treating paper evidence as reliable

1. **Unapproved intraday workflow is separate from router promotion.** `IntradayService` creates `intraday_regime_observer@1.0.0-paper` signals without consulting the immutable strategy approval registry. The router remains rejected. Explicitly designate experimental/demo evidence or integrate promotion gates; do not count experimental trades as approved router performance.
2. **Accounting paths diverge.** Intraday approval/exit update account/positions directly instead of the canonical fills/order ledger. Intraday fees/taxes are omitted; daily trade updates omit daily realized-P&L accumulation and use cost-basis values, not current marked prices. Operations uses `starting_equity + realized_pnl`; the no-ledger fallback adds realized P&L to cash, potentially double-counting it. Unify ledger writes and implement verified mark-to-market snapshots before relying on equity/drawdown.
3. **Risk enforcement is incomplete on intraday approval.** It sizes at signal time, forces a minimum quantity of one even if risk budget cannot afford it, and lacks the full portfolio/daily-loss/risk-engine gate path at approval. Halt bypass is fixed here, but sizing, caps and concurrent position controls still require integration tests.
4. **Data integrity is insufficient for intraday evidence.** One-symbol quote sampling, not complete OHLCV bars; last 30 observations can span sessions/gaps. Missing timestamps default to request time; bid/ask fall back to last price. Future-dated quotes are not rejected consistently in execution. Add source validation, timestamps, same-session history and gap handling.
5. **End-of-day recovery is incomplete.** Exits skip stale quotes, reconciliation counts only signals created that day, and the schedule checks weekdays rather than an authoritative exchange calendar. Carryover positions need explicit incidents and recovery; a stale price must not be fabricated into a fill.

### P1 — operational and product gaps

6. **Research management:** no dashboard job queue, backtest rerun, evidence artifact ingestion, immutable version comparison, or approval submission UI. The research API itself returns a checked-in snapshot, not a live experiment registry.
7. **Safety/auth:** rotate credentials previously shared in chat and replace weak admin credentials; verify session revocation, MFA/role boundaries and approval identity. Signal listing/review needs consistent account scoping before supporting more than one account. Resume currently accepts an operator label from the request body.
8. **Alerts:** signal emails have a single delivery attempt after signal commit, without transactional outbox/retry. Watchdog logs local readiness; it does not deliver outage alerts or monitor the public tunnel path. Add retries, delivery visibility, external health checks and escalation.
9. **Failure-test evidence:** operations currently returns static `passed` labels, including restart. Replace with persisted drill records containing timestamps, environment, assertions and artifacts. Unit tests do not prove production outage recovery.
10. **Audit completeness:** automatic signal expiry lacks a per-signal audit event; expired manual review writes then raises within the transaction, rolling its update back. Reconciliation and approval must be checked against actual ledger records.
11. **RAG/intelligence:** the dashboard retrieves cited text, not a trained kiwiT model or an independently validated adaptive strategy-selection model. Strategy invention/validation cannot be assumed from the presence of a research search box.
12. **Deployment reliability:** stable hostname, backup/restore verification, monitoring and recorded recovery drills are still needed. Do not enter credentials over raw HTTP.

### Intentionally unavailable

- Real Groww buy/sell/cancel actions.
- Promotion of the rejected router.
- Approve/reject when no pending signal exists.
- An isolated demo-signal sandbox (not yet built).

## Verification and limits

- Python suite: includes regression coverage for the previously bypassed intraday halt.
- Node built-in tests: navigation, partial API failures, polling/no overlap, pending-only controls, request timeouts/no automatic POST replay, and stale-age updates. These use a mocked DOM/transport, not pixel-level browser interaction.
- Live signed-in browser inspection was previously blocked by the browser policy check; do not claim a completed visual end-to-end audit based on these tests.
- No market orders or paper approvals were submitted as part of the audit.

## Recommended next implementation order

1. Repair accounting, approval gates, sizing, timestamp integrity and carryover handling together; validate with database-backed scenarios.
2. Add isolated demo workflow and actual evidence/drill registry.
3. Implement durable alerts and stable HTTPS hostname.
4. Start a clearly designated 4–8 week paper-evidence campaign only after the above acceptance tests pass.
5. Separately approve any future tightly capped live pilot.
