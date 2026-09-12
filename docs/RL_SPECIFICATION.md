# KIW-36: RL state, action and reward specification

Version: `rl-design-v1`. Status: **draft; experiments not authorized**. This is a proposed offline research contract, not an implemented environment or a validated strategy. KIW-33 operational activation/validation and KIW-34/35 full operational verification remain prerequisites. No live deployment belongs to this story.

## Scope and authority

The first experiment, when prerequisites pass, is one instrument, one session, at most one position, no leverage, no pyramiding and no same-step reversal. The policy selects an action; deterministic candidate eligibility, critic review, sizing, hard risk, session controls, kill switch and execution boundary retain veto authority. RL cannot change quantity, stops, targets, capital, costs, calendars, feature definitions, model artifacts or risk limits.

This design does not plug directly into `CandidateRanker`: that interface only permutes eligible candidate IDs and cannot express abstention or EXIT. A future environment/controller adapter must implement the action contract below; changing the ranker to smuggle actions into candidate IDs is forbidden.

## Observation at decision time t

Only records whose availability timestamp is at or before t may enter the observation. Feature versions, model fingerprints, calendar version and normalization parameters are frozen for each experiment. Normalizers are fit on training data only. Values have explicit missing masks and ages; missing signals never become valid zero scores. Unbounded numeric inputs are rejected before policy inference.

| Group | Values | Provenance and constraints |
| --- | --- | --- |
| Market regime | Fixed-order regime probabilities and availability mask | Registered specialist scored from current point-in-time snapshot |
| Opportunities | Trend, breakout and reversion scores, directions, eligible candidate masks | Full score envelopes and candidate IDs retained outside the numeric tensor |
| Volatility/trend | ATR divided by positive reference price, realized volatility, EMA distance | Existing feature engine; no future bars or revised unavailable data |
| Session clock | Fraction elapsed, seconds until entry cutoff and session end | Explicit exchange calendar; aware UTC storage and Asia/Kolkata session interpretation |
| Execution conditions | Spread fraction, quote age, known liquidity availability | Supplied executable quotes; no fabricated depth |
| Account/position | FLAT/LONG/SHORT/PENDING/EXIT_PENDING, quantity, age, entry-relative return, stop distance, cash/equity fractions | Durable paper ledger; agent cannot supply these values |
| Risk state | Realized and marked net P&L divided by episode starting equity, drawdown, remaining trade budget, loss streak | Simulator/accounting and independent risk policy |
| Context | Structured event risk, quant conflict, confidence, age and missing mask | Frozen advisory outputs; no raw prompts, credentials or unrestricted news text |

Rewards, future outcomes, exit labels, final session totals and subsequent revisions are not observation features. Model calibration/training cutoffs must precede evaluation. Arbitrary run IDs and dataset row numbers are excluded to reduce memorization of test sessions.

## Actions and external masks

Action IDs are fixed: `0 NO_TRADE`, `1 LONG`, `2 SHORT`, `3 EXIT`. NO_TRADE means no new order; an existing position continues under its original stops, targets and session policy. It does not mean cancel a pending exit.

- FLAT: LONG/SHORT are available only when a compatible eligible candidate exists and all prerequisite data/context checks pass. The highest ranked eligible candidate for that side is chosen deterministically. NO_TRADE is always available; EXIT is masked.
- OPEN: EXIT requests full closure; NO_TRADE retains the position. LONG/SHORT are masked. No reversal or risk increase is allowed.
- PENDING or EXIT_PENDING: only NO_TRADE is exposed until execution/reconciliation progresses. External safety actions may still cancel/close.
- Stale data, disabled trading, missing models or a halted account: entries are masked regardless of policy output. Exits remain governed by valid executable quotes and independent safety rules.

The environment records requested action, mask, selected candidate, downstream veto reasons, executed action and order IDs. Invalid/masked actions become NO_TRADE, produce no financial fill and receive no positive reward. Their rate is reported separately; recurrent invalid actions fail an experiment acceptance gate. A risk rejection must not be interpreted as an executed trade.

## Event order and episode boundaries

A decision step uses the established minute cadence. Apply newly available quotes to previously pending orders and existing stops/targets first; build the current snapshot; compute action masks; query the policy; run independent decision/critic/sizing/risk checks; submit any approved paper instruction. Newly submitted orders cannot fill on the quote already consumed in the same step. Intrabar stop/target order is never inferred from OHLC alone.

An episode begins at the configured observation time with fixed initial capital and a flat ledger. Include all eligible calendar sessions, including no-trade and losing days; do not select episodes based on outcomes. Daily resets are allowed only for this explicitly independent-session research task. A separate chronological evaluation must preserve cross-day capital, loss limits and drawdown; report both without conflating them.

At entry cutoff, masks forbid new entries. At session close, the external controller cancels pending entries and requests closure using the fixed simulator policy. Continue settlement events without policy entry actions until all positions and costs resolve. A missing final quote creates an UNRESOLVED episode; do not zero its position, invent a close, discard its losses or silently include it as a completed training trajectory. Report unresolved counts and conservative marked exposure separately. A configurable data-recovery timeout truncates collection and invalidates the affected training batch until reconciled. Crashes restore ledger state and replay idempotent events; unsupported orchestration recovery is an experiment blocker.

## Reward accounting

Let E0 be positive starting equity, frozen at reset. Let N_t be cumulative **net realized P&L**, including fees, taxes and slippage actually attributed to closed trades. Let U_t be marked unrealized P&L after incurred entry charges, using conservative executable-side marks. Define V_t = N_t + U_t. Let D_t be running peak-to-current equity drawdown divided by that running peak, and M_t = max(M_(t-1), D_t), initialized to zero. Let Q_t count filled entries; rejected submissions do not increment it.

Proposed reward:

`r_t = (N_t - N_(t-1) + U_t - U_(t-1)) / E0 - lambda_dd * (M_t - M_(t-1)) - lambda_trade * (Q_t - Q_(t-1))`

The unrealized term is a potential difference: for a fully settled flat episode, its sum is zero. Thus undiscounted total reward equals net realized episode return minus the maximum-drawdown and per-entry penalties. Initial experimental coefficients are `lambda_dd=1`, `lambda_trade=0.0001` (one basis point of initial equity per filled entry); freeze them before evaluation and include zero-penalty and higher-cost sensitivity comparisons. These values are research hypotheses, not empirically tuned defaults.

Use undiscounted episodic return (`gamma=1`) initially so delaying loss realization cannot improve the accounting identity. Any alternative discounting or reward scaling needs a new version and explicit tests of timing incentives. Do not separately subtract fees/slippage already included in N/U. No bonus for action count, confidence, unrealized profits, a successful API response or keeping a position open. No reward clipping that hides tail losses. Observation and reward normalization may use training-only statistics, but reports retain untransformed currency results.

### Reward-hacking invariants and examples

| Attempt | Required protection/test |
| --- | --- |
| Hide a losing position at episode end | No completion/reset until settlement; unresolved episodes remain visible and cannot count as profitable |
| Repeatedly open/close at an unchanged price | Spread, slippage and charges make net P&L nonpositive; each filled entry adds its penalty |
| Delay realizing a loss | V changes on conservative marking; undiscounted telescoping identity holds after settlement |
| Collect positive mark reward then close at the same mark | Realized/unrealized transfer contributes zero incremental reward before exit costs |
| Inflate size or remove stops | Action has no sizing/stops fields; deterministic risk rejects violations |
| Exploit optimistic candle ordering | Quote-driven fills; missing executable sequence cannot be replaced with best-case OHLC ordering |
| Reset capital/drawdown after a loss | Reset is environment-owned; continuous cross-session evaluation retains financial state |
| Double count a fill or a fee | Durable order/fill identities; sum of rewards reconciles exactly to ledger accounting |
| Learn future context or test labels | Availability-cutoff tests and held-out embargo checks; labels never reach inference |

For example, a settled gross profit of 100 with total execution costs of 20 produces N=80. With E0=10000, M=0.002 and one filled entry, total reward is `0.008 - 0.002 - 0.0001 = 0.0059`. A flat NO_TRADE episode with no account costs returns zero. Zero is a legitimate baseline; do not reward activity to force the agent to trade.

## Baselines and evaluation

Evaluate identical frozen sessions, costs, starting state, eligible candidate set and execution constraints for:

1. Always NO_TRADE (cash baseline).
2. Current deterministic confidence/strategy-priority meta policy with its unchanged exits.
3. Each specialist independently, subject to the same context and risk constraints.
4. Seeded random choice over valid actions, for environment diagnostics only.

Use chronological train/validation/untouched-test partitions with an embargo covering feature lookback and label/settlement horizons. For walk-forward work, record each training cutoff and evaluate strictly later windows. Predeclare seeds, hyperparameter/search budget, model versions, fees, quote coverage and reward coefficients. Test data cannot select coefficients, checkpoints, masks or baselines.

Report net P&L, expectancy, drawdown, turnover/costs, activity, invalid-action rate, veto rate, unsettled episodes, regime/session breakdowns and variability across seeds. Compare against the best eligible baseline with session-level paired uncertainty estimates; a few profitable trades or training reward is insufficient. Stress missing/stale quotes, worse spreads/slippage, model failures, halts and restart attempts. Promotion remains a separate manual governed decision under KIW-32. No live adapter, credentials or live-order client is exposed to the policy.

## Experiment release checklist (not yet satisfied)

Before implementation/training: restore and run the complete test environment; validate KIW-33 calendar/lifecycle and KIW-34/35 operations/broker controls; validate real historical tape and simulator reconciliation; implement the independent action/mask adapter; test all accounting invariants above; freeze evaluation manifests and baseline runs. This document alone satisfies no operational or performance gate.
