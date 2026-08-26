# One-click experimental paper sessions

Enter a simulated INR allocation, session loss percentage and profit percentage; click
**Run paper session** once per trading day. This creates a durable, attributed approval
for the `observer-session-v1` deterministic router, automatic simulated entries and exits.
There is no per-trade approval dialog. Closing the browser does not stop the EC2 timer.
**Stop & exit** revokes entries and requests liquidation at the next fresh market quote.
It must not be described as an immediate fill or guaranteed maximum loss.

The deployed dashboard uses `kiwit-paper-auto`, a separate ₹10,00,000 simulated ledger.
Your Run amount allocates only part of this virtual balance. `kiwit-paper-main` and its
old demonstration holding/history are preserved, not liquidated or reset. No real funds
are deposited, transferred or reserved at Groww. The auto account produces no signals
until a day-scoped Run approval exists.

## Exact boundaries

- Paper-only, currently the configured cash-equity/ETF symbol list (default NIFTYBEES).
  Not options. No broker order API is enabled.
- 09:30–15:45 Asia/Kolkata weekday service; no entries from 15:00, liquidation from
  15:10, no fills at/after 15:15, reconciliation after 15:30. Conservative cutoff
  avoids assuming continuous cash-market trading throughout a closing auction.
  Special sessions/holiday calendar are not implemented: missing fresh quotes blocks trades.
- Day-scoped approval; no renewal on the next day. A duplicate Run with the same
  parameters returns the same record; different parameters are rejected. A completed
  run cannot be restarted to reset its risk budget.
- The percentages apply to the allocated run amount using realized + marked open
  P&L after estimated costs. They also set each position's stop/target percentages.
- Max 25% allocation and 0.5% budget risk per entry, at most 10 entries/day, integer
  quantities, no leverage, no forced minimum one-unit trade. One pending/open signal
  per symbol; five-minute cooldown after exits. Gains cannot expand the initial budget.
- Estimated friction: 5bps fill slippage, 10bps costs/side. This is not an exact tax/
  broker charge calculation or a guarantee of obtainable prices. No liquidity/partial-fill model.
- Breakout: current exceeds previous ten observations' high and 5-observation mean
  exceeds 20-observation mean. Range: means within 0.3%, two-change RSI below 15.
  Otherwise cash. At least 20 contiguous same-day observations, maximum 120s gaps.
  These are sampled quotes, not exchange candle bars. This is not a trained LLM router.
- Missing, future or >120s-old quote timestamps fail closed. Stops wait when prices
  are stale or market is closed; unresolved positions remain visible and block a new run.
- Account/global safety halts still block entries; exits remain available.
- Manual reviews, worker ticks, and run/stop use account-scoped transaction advisory
  locking. Immutable approval and activity records link each automatic fill to its run.
- Database outages stop the worker; no synthetic local fills. Recovered workers load
  the persisted run and reconcile rather than creating a fresh approval.

## Evidence boundary

Experimental session consent does NOT promote the rejected Regime Router v1, change
its backtest gates, or establish live readiness. Research promotion remains locked.
Session P&L is shown separately; the older operations-report pipeline is not a complete
performance report for these experimental sessions. Further reconciliation/reporting,
precise fees, exchange calendar, alert retries, liquidity simulation and operational
failure drills remain before this could be considered production-ready.

The broker's daily authentication approval may still require the operator's action.
Run cannot bypass Groww authentication. Notification emails are not a dependency for
run execution; run activity is recorded in the dashboard audit, with no per-signal emails.

## Verification

`KIWIT_TEST_DATABASE_URL` enables PostgreSQL integration tests in a randomly named
schema inside a rollback-only transaction. CI supplies a disposable PostgreSQL service.
Tests cover start/stop, idempotency, approval provenance, automatic entries and exits,
stale exit handling, mark-to-market limits, costs, end-of-day exit, quote continuity,
and service recreation. They do not represent a real EC2 reboot or a broker execution test.

References: [NSE timings](https://www.nseindia.com/static/market-data/market-timings),
[Groww quote fields](https://groww.in/trade-api/docs/curl/live-data).
