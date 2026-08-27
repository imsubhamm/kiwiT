# Bank Nifty AI paper pilot

This is a separate simulated ledger, not a promotion of the rejected cash router.
It uses OpenAI `gpt-5.6-terra` through Responses structured outputs. The model
receives fresh underlying observations, a short rolling history, six or fewer
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

AI calls are at most once per five-minute UTC slot, after five contiguous completed
one-minute index candles. Forming candles are excluded; latest close must be no
older than 120 seconds, and option quotes no older than 60 seconds. Independent position supervision runs each minute and before
AI calls. Model failure, insufficient budget, bad output or stale data never
falls back to a forced/rules-only entry. Disabling AI still allows risk exits.
Missing Groww credentials/quotes prevents fills and is shown as a blocker.

## Budget

The $20 trial uses an $18 application allowance and $2/day limit, leaving a buffer.
Each call durably reserves $0.20 before network I/O under a database lock. Successful
calls reconcile to a conservative $5/M input + $30/M output accounting rate, above
the verified Terra $2/$12 rate (2026-08-26). The payload is limited to 20k bytes,
output to 1k tokens; no tools or retries. Ambiguous failures retain the reservation.
This is NOT the provider invoice or account-wide enforcement: other applications,
tax, pricing changes and manual API calls are outside this ledger. Review pricing
before changing models. Keep provider auto-recharge off. All model decisions and
input snapshots are in `banknifty_ai_calls`; session/fills in `banknifty_events`.

## Versioned automatic playbooks

The selector now supplies explicit versioned entry plans for opening-range breakout,
breakout/retest, trend pullback and range reversal. The AI chooses a supplied plan
or waits; execution independently rechecks the current underlying and option prices.
New positions also carry underlying invalidation and time exits. The dashboard
shows eligibility reasons, plans, rejected decisions and partial-fill-aware paper
results per playbook. See [BANKNIFTY_PLAYBOOKS.md](BANKNIFTY_PLAYBOOKS.md) for the
exact routing/entry rules and remaining historical-options validation requirements.
No eligible plan while flat means no paid AI call. Existing exits still run.

## Risk and limitations

- One long position, whole lots from the current Groww master, no expiry-day entries.
- Entries 09:30–15:00 IST, flatten from 15:15, no fills at/after 15:30.
- Max 10 entries, 25% premium allocation, 1% initial capital at planned stop.
- User percentages apply to session net P&L and individual premium stops/targets.
- Ask-side buys, bid-side sells, 10bps adverse slippage rounded to tick, illustrative
  20bps + ₹20 per fill fees. These are NOT exact options brokerage/tax calculations.
- Displayed depth limits quantity; partial exits persist. No stale or invented fills.
- Five-minute cooldown after exits, immutable daily limits, durable stop/restart state.
- Holidays have no explicit calendar yet: stale candles/quotes block trading.
  Groww's index quote was verified to lack a trade timestamp. Instead, the adapter
  uses its documented `/v1/historical/candles` endpoint and completed candle close
  times, never receipt time. During-market freshness still needs a forward check.
- No options backtest, profitability claim, realistic queue simulation or live readiness.
- Closed-market residual positions require attention; never fabricate an EOD close.
- Existing cash portfolio cards do not include this isolated options ledger.

Sources: https://groww.in/trade-api/docs/curl/instruments and
https://groww.in/trade-api/docs/curl/live-data ;
https://developers.openai.com/api/docs/guides/structured-outputs ;
https://developers.openai.com/api/docs/models/gpt-5.6-terra

## Connectivity check, 2026-08-26

One synthetic no-data API call returned HOLD: 272 input tokens, 39 output tokens.
At documented $2/$12 per million rates the estimated generation charge is $0.001012
before any cache effects/taxes; conservative trial accounting equivalent is $0.00253.
This was a connectivity check outside the session ledger, not a trade or performance
test. No trading session was started. The $2 buffer covers this small setup call.
