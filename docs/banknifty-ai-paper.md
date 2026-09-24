# Bank Nifty AI paper pilot

This is a separate simulated ledger, not a promotion of the rejected cash router.
It uses OpenAI `gpt-5.6-terra` through Responses structured outputs. The model
receives fresh underlying observations, a short rolling history, up to ten
near-ATM next-expiry contracts and the current position. No books are uploaded,
no fine-tuning occurs, and no news or historical edge is claimed. Model summaries
are recorded, not hidden chain-of-thought. BUY is long premium only; EXIT cannot
open a short. The deterministic boundary can reject any model decision.

The chart-evidence extension now adds five prior complete observed sessions,
plus the distinct previous calendar week's Monday–Friday trend and coverage,
completed 1m/5m/15m analysis, explicit setups and dashboard candles. New entries
require fresh matching chart evidence. See [CHART_ANALYSIS.md](CHART_ANALYSIS.md)
for precise rules, coverage, caching and remaining limitations. Raw chart bars
remain in the session; compact evidence is sent to the model within the existing
request-size and API-budget limits.

## Enable only after migration, tests and feed verification

1. Apply migration 010 with the existing migration tool.
2. Set `OPENAI_API_KEY` securely in `/etc/kiwit/kiwit.env` (never Git or browser).
3. Set `KIWIT_BANKNIFTY_AI_ENABLED=true` there. Groww read-only credentials required.
4. Install `deploy/kiwit-banknifty.service` and `.timer` in systemd; reload and enable
   the timer. Restart the API for environment changes. A key alone does not enable it.
5. Dashboard → Bank Nifty AI paper desk → capital / loss / target → Run.
   No session is started by deployment. New consent is required each trading day.

For GitHub deployments, set the repository secret `OPENAI_API_KEY` and variable
`KIWIT_BANKNIFTY_AI_ENABLED=true`; otherwise the workflow deliberately leaves this
desk disabled. The deployment script installs the independent timer. The legacy
cash Run endpoint is blocked when this feature is enabled; old cash exits/history
are preserved. Existing portfolio/research cards remain explicitly separate.

AI calls are event-driven and retain the two-minute slot as a concurrency backstop.
Stable eligible plans do not cause repeated paid calls; a changed plan set, price/chase
band, or near-stop/near-target position band creates a new decision event. Cooldown,
entry-count, session-loss, one-position and quantity gates run before reservation.
Calls begin only after five contiguous completed one-minute index candles. Forming candles are excluded; latest close must be no
older than 180 seconds, and option quotes no older than 90 seconds. Independent position supervision runs each minute and before
AI calls. Model failure, insufficient budget, bad output or stale data never
falls back to a forced/rules-only entry. Disabling AI still allows risk exits.
Missing Groww credentials/quotes prevents fills and is shown as a blocker.

## Budget

The application ledger allows $50 per rolling 30 IST calendar days and $5/day. This is an internal ceiling,
not a provider invoice or an automatic credit purchase.
Each call durably reserves $0.20 before network I/O under a database lock. Successful
calls reconcile to a conservative $5/M input + $30/M output accounting rate, above
the verified Terra $2/$12 rate (2026-08-26). The payload is limited to 20k bytes,
output to 1k tokens; no tools or retries. Ambiguous failures retain the reservation.
This is NOT the provider invoice or account-wide enforcement: other applications,
tax, pricing changes and manual API calls are outside this ledger. Review pricing
before changing models. Keep provider auto-recharge off. All model decisions and
input snapshots are in `banknifty_ai_calls`; session/fills in `banknifty_events`.

## Versioned automatic playbooks

Selector v4 supplies explicit versioned entry plans for opening-range breakout,
breakout/retest, trend pullback, range reversal, previous-day breakout, engulfing,
hammer and shooting-star reversal. The AI chooses a supplied plan
or waits; execution independently rechecks the current underlying and option prices.
New positions also carry underlying invalidation exits. The dashboard
shows eligibility reasons, plans, rejected decisions and partial-fill-aware paper
results per playbook. See [BANKNIFTY_PLAYBOOKS.md](BANKNIFTY_PLAYBOOKS.md) for the
exact routing/entry rules and remaining historical-options validation requirements.
No eligible plan while flat means no paid AI call. AI EXIT is advisory evidence;
deterministic premium stop/target, underlying invalidation, session and end-of-day
rules retain exit authority.

## Risk and limitations

- One long position, whole lots from the current Groww master, no expiry-day entries.
- Entries 09:30–15:00 IST, flatten from 15:15, no fills at/after 15:30.
- At or immediately after 15:30 IST the worker creates one immutable daily paper report,
  stores it in PostgreSQL, displays it on the dashboard and attempts email delivery.
  Delivery uses a durable claim lease and retries after 15 minutes; SMTP is at-least-once. If a fresh executable
  quote was unavailable, the report explicitly marks the position unresolved instead
  of inventing a closing fill.
- Max 10 entries, 25% premium allocation, 1% initial capital at planned stop.
- User percentages apply to session net P&L and cap individual premium risk. Selector
  v5 assigns a versioned stop, net reward multiple and 20/30/45-minute holding deadline
  per playbook. The session trade stop remains the maximum permitted premium stop.
- Ask-side buys and bid-side sells retain 10bps adverse slippage rounded to tick.
  Costs use the versioned `groww-nse-equity-options-2026-04-01` schedule: ₹20 per
  executed order plus exchange, IPFT, SEBI, GST, stamp-duty and sell-side STT
  components. A plan is rejected when estimated round-trip costs exceed 35% of its
  planned gross reward. Take-profit prices are raised to cover estimated entry/exit
  costs and the playbook's minimum net reward multiple. Contract notes remain
  authoritative; import their actual costs with
  `python scripts/import_broker_costs.py costs.csv`.
- Displayed depth limits quantity; partial exits persist. No stale or invented fills.
- Five-minute cooldown after exits, immutable daily limits, durable stop/restart state.
- The entry cap, cooldown and session P&L limit block execution but keep minute-by-minute
  selector scans in shadow mode until the entry window closes.
- Every eligible contract is retained for at least 20 minutes; opened-position contracts
  are retained through the session. Provider IV, Greeks, volume and OI fields are stored
  when present, and missing coverage is explicit. No IV or Greek is invented.
- Set `KIWIT_OPTIONS_EVENT_CALENDAR` to a point-in-time JSON file matching
  `config/options-event-calendar.example.json`. Verified high-impact events within two
  hours block entries. The file requires an owner, source reference and timezone-aware
  `as_of`; after 30 days it becomes invalid. Unconfigured, invalid or non-clear
  coverage blocks entries before AI reservation while shadow scans continue.
- Regular-session holidays use the versioned 2026 NSE F&O calendar; unknown years block entries.
  Groww's index quote was verified to lack a trade timestamp. Instead, the adapter
  uses its documented `/v1/historical/candles` endpoint and completed candle close
  times, never receipt time. During-market freshness still needs a forward check.
- Offline evaluation reports the 4/6/10 entry-cap × 2%/3%/5% loss matrix and
  playbook-specific holding horizons on the same retained quote tape. These are
  overlapping opportunity studies, not portfolio returns or profitability claims.
- No profitability claim, realistic queue simulation or live readiness.
- Closed-market residual positions require attention; never fabricate an EOD close.
- Existing cash portfolio cards do not include this isolated options ledger.

Sources: https://groww.in/trade-api/docs/curl/instruments and
https://groww.in/trade-api/docs/curl/live-data ;
https://groww.in/pricing/futures-and-options ;
https://www.nseindia.com/static/products-services/equity-derivatives-securities-transaction-tax ;
https://developers.openai.com/api/docs/guides/structured-outputs ;
https://developers.openai.com/api/docs/models/gpt-5.6-terra

## Connectivity check, 2026-08-26

One synthetic no-data API call returned HOLD: 272 input tokens, 39 output tokens.
At documented $2/$12 per million rates the estimated generation charge is $0.001012
before any cache effects/taxes; conservative trial accounting equivalent is $0.00253.
This was a connectivity check outside the session ledger, not a trade or performance
test. No trading session was started. The $2 buffer covers this small setup call.

See [OPTIONS_OPERATIONS.md](OPTIONS_OPERATIONS.md) for independent observation/supervision, recovery classification and delivery catch-up.
