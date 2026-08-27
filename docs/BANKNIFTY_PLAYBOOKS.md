# Bank Nifty paper selector v1

Implementation: `banknifty-selector-v1`; sessions created by this release use
`banknifty-ai-v4-playbooks`. All four playbooks are **unvalidated paper experiments**.
This is not promotion of the rejected cash router or permission for broker orders.
No new strategy is invented or automatically promoted each morning.

## Fixed catalogue and routing

| Versioned playbook | Existing completed 5m setup | Required 5m and 15m regimes | Maximum hold |
| --- | --- | --- | --- |
| opening_range_breakout_v1 | First 15m range breakout | Both match direction | 45 minutes |
| breakout_retest_v1 | Prior 20-bar breakout and retest | Both match direction | 45 minutes |
| trend_pullback_v1 | EMA9 pullback and directional close | Both match direction | 45 minutes |
| range_reversal_v1 | Rejection back inside the prior 20-bar range | Both range | 30 minutes |

See CHART_ANALYSIS.md for exact setup formulas. The weekly context must be complete;
an opposing directional weekly bias rejects the direction. Mixed/range weekly
context is allowed. Missing/stale evidence, unsupported regimes and no pattern
produce an explicit wait reason. Hammer/engulfing shapes alone do not qualify.
These thresholds are engineering hypotheses, not fitted confidence or measured edge.
Changes to rules require a new selector/playbook version and new evaluation.

## Explicit entry plans

At most one plan per playbook, four overall. Eligible contracts come from the
existing read-only Bank Nifty option universe. Filter direction, fresh executable
quotes, spread <=2%, non-expiry-day contracts and affordable whole lots. Order by
relative spread, then distance to spot, then symbol; take the first affordable
contract. The AI selects among these bounded plans or HOLD, not arbitrary contracts.

Each plan records a content-hashed ID, selector/playbook versions, source setup and
timestamp, contract, quantity, trigger, invalidation, maximum chase price, premium
cap, indicative stop/target, holding limit and expiry. Trigger is the setup's
observed close; permitted price is from trigger to 0.5 five-minute ATR beyond it
in the entry direction. Invalidation must be strictly on the opposite side.

Expiry is the earliest of creation +90 seconds, setup +300 seconds and analysis
+120 seconds. Premium cap allows at most 0.5% ask movement, then the existing adverse
slippage/tick rounding. Quantity is sized at that cap and never increased at fill.
Sizing retains max25% premium allocation and max1% initial-capital planned risk,
and now also respects the remaining daily loss budget after realized P&L.
Illustrative fees, slippage and stop gaps mean these are not guaranteed loss caps.

Before filling, fetch completed underlying candles again and a fresh option quote.
Reject an unknown/mismatched/tampered/expired plan, older underlying evidence,
invalidated trigger, excessive chase, excessive premium, lost liquidity or reduced
cash/risk capacity. Existing session lock, halt, window, cooldown, expiry-day,
one-position and once-per-call checks remain. Rejections are linked to their AI call
and stored as `rejected` with a validation error rather than left `completed`.

Stops/targets are calculated from the **actual simulated fill**, not the earlier
indicative premium. AI does not set size, prices or risk limits.

## Model and independent exits

The existing model, budget and endpoint are unchanged. The strict structured output
adds a required `plan_id`: BUY must reference a supplied plan and its symbol/strategy.
HOLD uses empty symbol/plan ID and `no_trade`; EXIT references the held symbol with
empty plan ID and `no_trade`. Instructions request a concise choice explanation,
not hidden reasoning or invented win probabilities. Flat with no eligible plan
means no paid AI call; an open position still permits AI HOLD/EXIT decisions.

Independent premium stops, session limits and EOD exits remain. New positions also
exit on a fresh post-entry completed underlying candle crossing plan invalidation,
or their holding deadline. Underlying-feed failure does not disable premium/time
exits; executable option quotes are still required. Monitoring is minute-based,
not tick-level or guaranteed instantaneous. Broker mutation methods remain disabled.
Legacy positions without a plan retain their original exit behavior.

## Persistence and dashboard

Existing JSONB storage holds selection snapshots, all four eligibility explanations,
plans, selected plan and the exact AI input. Each scan is an audit event. Paper
entries preserve the plan; exits include position ID, playbook version, net realized
P&L and whether the position is fully closed. Status exposes the current selection,
catalogue, rejection explanations and all-time version-attributed paper results.

Evidence aggregates partial fills per position before counting closed trades/winners.
Entry cost remaining is persisted and apportioned on partial exits; the final fill
uses the exact remainder so event P&L reconciles to session realized P&L without
repeated per-unit division drift.
Closed net P&L and realized P&L including partial exits are displayed separately.
Old events without attribution are not silently assigned to a playbook. The review
is not mark-to-market drawdown, a historical backtest or promotion approval.
UI displays expiry/staleness and uses text-only rendering for external/model strings.
The user's action remains capital + limits + Run; no per-trade buttons are added.

## What is and is not validated

Automated tests cover routing, both directions, weekly conflicts, malformed AI
decisions, plan mutation, latency/expiry, price movement, stale data, liquidity,
budget, duplicate requests, stop races, independent exits and partial-fill accounting.
No live order or paid AI call is required for these tests.

Profitability validation is **not complete**. It requires point-in-time option
contracts and executable historical bid/ask data, realistic costs, walk-forward
out-of-sample testing of each playbook **and the complete AI selector**, followed by
forward paper evidence. Underlying-index candles alone cannot establish option P&L.
Record dataset/version, model/prompt version, selection decisions and baseline
results before interpreting performance. No self-training or automatic live promotion.

Official API contract reference used for this implementation:
https://developers.openai.com/api/docs/guides/structured-outputs

## Daily learning v1

When a session finishes flat, an idempotent daily record stores its version,
realized P&L, entries and audit-event counts. The next day receives at most ten
prior daily summaries plus closed-trade results grouped by playbook. Under 20
trades is `collecting`; 20 or more is only `exploratory`. It may break a tie between
otherwise eligible plans but cannot override current evidence or risk. It does not
change model weights, code or limits, and cannot promote itself or enable live orders.
This is controlled retrieval-based learning, not training.

The design follows official OpenAI guidance to make changes incrementally and
re-evaluate them on representative evidence rather than assuming improvement:
https://developers.openai.com/api/docs/guides/latest-model
