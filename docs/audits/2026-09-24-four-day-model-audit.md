# Four-day Bank Nifty paper-model audit

Audit time: 24 September 2026, 12:09 IST

Evidence window: 21-24 September 2026. Monday has market data only through
10:33 IST because the observer became unavailable. Thursday is partial through
12:05 IST because the session was still running when the evidence was exported.

Deployed release: `9cd1a5f396269523b42b49cadb914c960fc90c1b`

Evidence export SHA-256:
`e9ebd4d94bb756b0f81120deb445775e369d8c06a54fe0c95a8439ed8664d0b3`

## Verdict

The paper execution and accounting system passed its main safety test: all 23
positions were closed, the first three daily reports were sent, current workers are
healthy, and all 47 entry/exit events from this window replay with matching fill and
accounting evidence. No live order was sent.

The strategy/model result failed provisionally. The four sessions produced
`-INR 7,105.576` on nominal daily capital of INR 100,000, with 3 profitable positions
from 23. The sample is too small for a profitability claim, but it is large enough to
reject promotion and to require rule changes before collecting another comparable
forward sample.

The model cannot yet be credited with reliably finding or avoiding missed
opportunities. At the 15-minute comparison horizon, 49 of 61 candidate comparisons
were excluded because the future quote/depth for the contract was no longer in the
five-strike tape. Only 10 comparisons were paired. This is an evidence-coverage
failure, not proof that the excluded opportunities won or lost.

## Four-session results

| Session | Observation coverage | Entries | Winners | Net paper P&L | Operational result |
| --- | ---: | ---: | ---: | ---: | --- |
| 21 Sep | 67 observations, 09:24-10:33 IST | 1 | 0 | -INR 879.046 | Report sent; afternoon market feed unavailable |
| 22 Sep | 338 observations, 09:51-15:29 IST | 9 | 2 | -INR 128.163 | Flat and report sent |
| 23 Sep | 362 observations, 09:28-15:29 IST | 10 | 1 | -INR 4,309.812 | Hit 10-entry cap; flat and report sent |
| 24 Sep | 154 observations, 09:28-12:01 IST | 3 | 0 | -INR 1,788.555 | Partial session; running at audit cutoff |

Aggregate metrics:

- 23 closed positions: 3 winners and 20 losers.
- Win rate: 13.0%.
- Net expectancy: `-INR 308.94` per position.
- Average winner: `INR 1,458.57`; average loser: `-INR 574.06`.
- Realized payoff ratio: 2.54 to 1, requiring approximately a 28.2% win rate to
  break even before sampling uncertainty. Observed win rate was 13.0%.
- Profit factor: 0.38.
- Gross option-price P&L was `-INR 5,133.00`. The illustrative fees and fill model
  reduced the result by another `INR 1,972.576`.
- Tuesday was gross-positive by `INR 616.50`, but illustrative costs of
  `INR 744.663` changed it to a net loss. The cost model therefore materially changes
  conclusions and must be reconciled to broker evidence.

## What the model did well

1. It respected the bounded schema after release `9cd1a5f`. Tuesday onward had no
   HOLD/EXIT response-shape failures. One provider HTTP failure on Wednesday recovered
   without producing a trade.
2. It did not invent contracts, quantities, fills, stops or targets. Independent code
   retained authority over those values.
3. It selected a non-first plan only once. Most decisions were conservative HOLDs,
   and it did not use the existence of an eligible plan as an automatic order.
4. In the small observable 15-minute paired sample, two HOLD decisions avoided
   deterministic first-plan losses of approximately INR 2,377 and INR 385. Across nine
   paired opportunities in the current experiment, AI was INR 2,761.937 better than
   the first-plan baseline. This is directional evidence only, not validation.
5. It exited five positions before a stop/target condition, and the independent
   supervisor continued to enforce stops, targets and underlying invalidation.

## What failed and why

### 1. The selector produced negative expectancy

| Playbook | Trades | Winners | Net P&L |
| --- | ---: | ---: | ---: |
| `trend_pullback_v3` | 7 | 1 | -INR 2,574.190 |
| `opening_range_breakout_v3` | 2 | 0 | -INR 1,481.810 |
| `engulfing_reversal_v3` | 6 | 1 | -INR 1,434.314 |
| `previous_day_breakout_v3` | 2 | 0 | -INR 754.796 |
| `range_reversal_v3` | 1 | 0 | -INR 464.728 |
| `breakout_retest_v3` | 4 | 1 | -INR 261.013 |
| `shooting_star_reversal_v3` | 1 | 0 | -INR 134.725 |

No playbook has enough observations for promotion. `trend_pullback_v3`,
`opening_range_breakout_v3` and regime-light reversal entries are the immediate
negative contributors.

The fixed 5% premium stop and 10% target produced nine stop-loss exits and three
take-profit exits. Six underlying-invalidation exits and five AI-exited positions were
also all net losers. Outcome totals by terminal cause were:

- take profit: 3 positions, `+INR 4,375.719`;
- stop loss: 9 positions, `-INR 5,619.666`;
- underlying invalidation: 6 positions, `-INR 3,100.140`;
- AI exit: 5 positions, `-INR 2,761.489`.

This does not prove the exits were wrong because the tape does not retain enough
post-exit contract quotes to compare them with continued holding.

### 2. The AI mostly rubber-stamped the deterministic selector

There were 52 BUY decisions. Forty-four had only one eligible plan and eight had two.
The AI selected a plan other than the first deterministic plan only once. Its useful
role in this sample was vetoing, not ranking or contract selection.

The system made 201 paid/attempted calls: 52 BUY, 138 HOLD, 5 EXIT and 6 failures.
Those calls used approximately 992,453 tokens and charged `USD 5.97074` to the
conservative internal budget. Ninety calls on Tuesday and 83 on Wednesday show that
fixed two-minute slots create many repetitive HOLD decisions.

### 3. The model made factual numeric/time mistakes

The two observable profitable vetoes cannot yet be treated as reliable reasoning:

- On 22 September it said spot `56141.75` had rebounded above invalidation
  `56154.55`; numerically it was below that level.
- On 23 September at approximately 12:20 IST it said a plan expiring at 12:22 IST had
  already expired.

Both HOLDs happened to avoid a losing 15-minute baseline, but the explanations show
that the language model must not perform authoritative arithmetic, timestamp or plan
validity checks. These must be supplied as deterministic computed fields.

### 4. More than half of BUY decisions were rejected after paying for AI

Only 23 of 52 BUY decisions became entries. The other 29 were rejected:

- 15: combined spread, quote-freshness or maximum-price check;
- 9: five-minute post-exit cooldown;
- 4: quantity no longer fit cash, risk or liquidity;
- 1: unknown plan selection.

The cooldown and known capacity rules should be evaluated before reserving AI budget.
They are valid safety rules but are currently placed too late in the funnel. Rejected
calls consumed approximately `USD 0.925`, about 15.5% of four-session AI allowance.

The combined quote rejection text prevents diagnosis. It must record separate safe
codes and values for spread, quote age and price cap.

### 5. Monday lost most of its market session

The observer produced data through 10:33 IST and then returned `unavailable` until
market close. The decision worker recorded 264 stale-observer blocks. EC2 journals
show the timer continued running, so this was a repeated market-snapshot failure rather
than a stopped timer. The implementation catches broker, validation and arithmetic
errors under one `MARKET_SNAPSHOT_UNAVAILABLE` category, so the original cause is no
longer recoverable.

The five response-validation failures on Monday happened before release `9cd1a5f`.
The deployed fix removed that failure from Tuesday onward.

### 6. The evidence system cannot measure most missed opportunities

At a 15-minute horizon, only 10 of 61 comparisons were paired. Forty-nine lacked a
future entry quote/depth record, one lacked a future exit quote/depth record and one
selected plan was unknown. The five-nearest-strike moving universe drops contracts
after spot moves.

After the tenth entry closed on Wednesday at about 14:30 IST, the session immediately
completed. No more selector scans were stored even though the entry window continued
until 15:00. Therefore the effect of the 10-entry cap cannot be measured from this
tape.

## Areas the model does not cover

- implied volatility, volatility skew/surface, Greeks and theta/gamma exposure;
- option open interest, volume, change in open interest and multi-level order book;
- a contract's premium history and post-decision/post-exit path after it leaves the
  five-strike universe;
- event and macro risk such as RBI decisions, scheduled data and unexpected news;
- calibrated gap/exhaustion logic, volatility-normalized entry distance and
  playbook-specific market regime;
- actual broker taxes, exchange charges, slippage, latency and partial-fill evidence;
- portfolio-level capital compounding, cross-trade exposure and mark-to-market
  drawdown;
- statistically valid out-of-sample comparison against HOLD and deterministic
  baselines;
- causal reasons for market snapshot failures because exceptions are collapsed.

## Rule decisions

### Keep

Keep plan integrity, paper-only execution, fresh executable quotes, completed candles,
spread/depth checks, cash and risk limits, whole lots, one open position, verified
calendar, expiry protection, independent exits, daily halt and reconciliation. These
rules prevented fabricated fills and unsafe accounting. Their implementation should
be improved without removing the boundary.

Keep the cooldown as a risk boundary for now, but move it before the AI call. The same
applies to entry count, cash, remaining risk and quantity feasibility.

### Change before the next evaluation version

1. Remove numeric/time validity decisions from the model prompt. Provide computed
   booleans and distances such as `plan_valid_now`, `quote_age_seconds`,
   `spread_pct`, `fill_headroom_pct`, `invalidation_distance_atr`, `rsi_extreme` and
   `cooldown_remaining_seconds`.
2. Change `engulfing_reversal_v3` from “current chart evidence only.” Require a range
   or exhaustion regime and block a reversal against a strong opposing 15-minute
   regime. Six trades produced only one winner and `-INR 1,434.314`.
3. Add an exhaustion/chase filter to momentum entries. The model bought continuation
   after extreme RSI and large gap/extension observations, including Thursday's losing
   opening-range breakout. Compare 0.25 ATR versus the current 0.5 ATR chase allowance.
4. Replace one universal 5%/10% premium exit with versioned, playbook-specific tests:
   volatility/ATR stop, structural underlying invalidation, 5%/8%, and 5%/10%.
   Preserve a hard catastrophic stop in every version.
5. Reduce experimental exposure to 25% premium allocation and 1% planned risk until a
   playbook passes forward evidence. Larger 50%/2% sizing magnifies an unvalidated
   negative edge; it does not create the edge.
6. Compare daily entry caps of 4, 6 and 10 and daily loss stops of 2%, 3% and 5% on the
   same retained tape. Do not simply remove the cap. Wednesday's 10 entries lost
   4.31%, but Tuesday's later winners recovered earlier losses, so four days cannot
   identify the best threshold.
7. Replace fixed two-minute paid polling with event-driven calls: new plan, materially
   changed plan ranking, position threshold proximity or a minimum HOLD refresh
   interval. Continue one-minute deterministic supervision.
8. Retain every eligible, selected, opened and recently exited contract through the
   evaluation horizon. Continue shadow selector scans after cooldown, daily cap and
   session loss blocks so each rule's opportunity cost is measurable without trading.
9. Make AI EXIT advisory until post-exit counterfactual coverage exists, or require a
   deterministic corroborating condition. Five AI-exited positions were all losses,
   but current evidence cannot show whether those exits reduced larger losses.

### Do not remove based on this audit

- quote freshness, spread and price-cap recheck;
- five-minute cooldown;
- cash, quantity and planned-risk validation;
- one-position lock and daily loss halt;
- plan ID/content hash and selector-version checks;
- expiry-day protection and end-of-day flattening;
- AI budget accounting.

The daily `USD 5` allowance did not bind; full days used about `USD 2.60`. Increasing
it would not improve the observed selector and would add more repetitive calls.

## Implementation order

1. Add tracked-contract tape retention and post-limit shadow scans. Without this,
   missed-opportunity and rule-removal claims remain untestable.
2. Move cooldown, entry-count and sizing feasibility before AI reservation; split
   execution rejection codes.
3. Add exact observer failure categories, consecutive-failure alerts and recovery
   events. Preserve safe error text and never store provider bodies or secrets.
4. Create selector v4 experiments for reversal regime, exhaustion/chase and exit
   rules. Run every variant on the same frozen tape.
5. Reduce paper sizing while collecting the next forward sample.
6. Use event-driven AI calls and computed validity fields. Keep the model as a bounded
   veto/ranker rather than an arithmetic or execution authority.
7. Reconcile costs with actual broker contract notes before interpreting net edge.

## Promotion gate

Do not promote any playbook or enable live execution. Require at least:

- complete tracked-contract coverage with reported exclusions;
- chronological, non-overlapping portfolio evaluation;
- current selector versus deterministic first-plan and HOLD baselines;
- actual-cost and stressed-cost results;
- enough trades per playbook to estimate uncertainty;
- positive held-out expectancy and acceptable drawdown;
- a complete deployed observation-to-report acceptance chain;
- continued exact replay parity for selector and fills.


## Remediation implemented after this audit

Branch `codex/four-day-remediation` implements the follow-up controls:

- Retains every eligible contract for at least 20 minutes and opened-position contracts through the session so fixed-horizon and post-exit quotes remain available.
- Records categorized observer incidents and recoveries with safe error details instead of collapsing every failure to `unavailable`.
- Continues strategy scans in shadow mode after the daily entry cap, session P&L limit, cooldown, or other deterministic entry gate blocks execution.
- Preserves provider IV, Greeks, option volume and OI when supplied; records field-by-field coverage and volatility-surface sufficiency instead of inventing missing values.
- Adds a point-in-time event-calendar adapter. Verified high-impact windows block entries; missing or invalid calendar data stays explicitly unknown.
- Uses selector v4 with 0.25 ATR chase allowance, RSI exhaustion protection, and 5m/15m reversal confirmation.
- Restores 25% allocation and 1% planned-risk sizing.
- Moves cooldown, entry cap, session-loss, one-position and quantity feasibility ahead of paid AI inference.
- Calls AI only on a material eligible-plan or position-risk event. Stable opportunity state does not poll the model repeatedly.
- Makes AI EXIT advisory; deterministic stop, target, invalidation, session and end-of-day controls retain exit authority.
- Replaces the flat illustrative fee with the versioned `groww-nse-equity-options-2026-04-01` component schedule and adds contract-note cost import.
- Adds offline 4/6/10 entry-cap × 2%/3%/5% daily-loss comparisons and playbook-specific exit-horizon comparisons on retained tape.

The safety rules called out in the audit remain enforced: quote freshness, spread, fill cap, cooldown, cash/risk, immutable plan integrity, one-position, verified calendar, non-expiry-day entry, and end-of-day reconciliation.
