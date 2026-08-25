# TradingKIWI: Book Synthesis and Research Plan

Status: research draft - no broker connection and no live-trading authorization

## 1. Source inventory and limitations

| Source | Usable coverage | Main contribution |
|---|---:|---|
| Steve Nison, *Japanese Candlestick Charting Techniques* | Full, 298 pages | Candle context, reversal/continuation clues, confirmation, support/resistance, protective stops |
| Alexander Elder, *Trading for a Living* | Full, 306 pages | Psychology, multi-timeframe Triple Screen, indicators, stops, money management |
| Mark Douglas, *Trading in the Zone* | Full, 143 pages | Probabilistic thinking, consistent execution, sample-size evaluation |
| Jack Schwager, *Market Wizards* | Full, 165 pages | Cross-trader principles: risk control, undertrading, preplanned exits, adaptability |
| Van K. Tharp, *Trade Your Way to Financial Freedom* | Partial, 50 pages | System objectives, expectancy, R-multiples, stops, position sizing |
| Adam Grimes, *The Art and Science of Technical Analysis* | Table of contents only, 5 pages | Topic map only; insufficient evidence for detailed rules |
| Robert Carver, *Systematic Trading* notes | Partial notes, 349 lines | Simplicity, modular systems, forecast scaling, diversification, avoiding overfitting |

The last three sources are incomplete in the shared folder. No claim unique to those partial files will become a production rule without a complete source or independent empirical validation.

## 2. What the books agree on

1. Survival is the first objective. A system that can create a ruinous drawdown is invalid even if its average return looks attractive.
2. Risk is chosen before entry. The technically meaningful invalidation point determines the stop; position size is reduced to fit the risk budget.
3. No single pattern or indicator is an edge. Context, trend, confirmation, execution, costs, and exits jointly define the trade.
4. Trade in the direction of the higher-timeframe regime or stand aside.
5. Treat every trade as one event in a probability distribution. Judge a strategy over a sufficiently large sample, not by the last winner or loser.
6. Simple, explicit rules are preferable to discretionary complexity and are easier to test, follow, and audit.
7. Never average down, widen a stop to avoid realizing a loss, revenge-trade, or increase size after losses.
8. Correlated positions are one portfolio risk, not separate independent bets.
9. The system must fit the operator. A profitable backtest is useless if its drawdowns, frequency, or holding period cannot be followed consistently.
10. Markets change. Rules require monitoring, but changes must be evidence-driven and never made in reaction to a small losing streak.

## 3. Proposed scope for version 1

Version 1 will be deliberately narrow:

- Market: NSE cash equities and NIFTY 50 index data.
- Trading style: end-of-day swing trading; expected holding period 2-15 sessions.
- Direction: long-only during initial validation.
- Instrument for first live phase: liquid cash equities or a broad-market ETF, not options.
- Options: analyzed and paper-traded only until a separately tested options model passes all gates.
- Decisions: one daily scan after market close; no impulsive intraday signals.

This scope avoids early leverage, expiry-day noise, theta decay, implied-volatility modeling errors, and the larger contract exposure of Indian index derivatives.

## 4. Candidate strategy A: trend-pullback-confirmation

This is a codable adaptation of Elder's multi-timeframe structure and Nison's contextual confirmation.

### Universe filter

- NIFTY 100 constituents, survivorship-bias-free by date when historical membership is available.
- Median 20-day traded value above a configurable liquidity floor.
- Exclude symbols under exchange surveillance, trading bans, or with missing/corrupt data.
- Exclude new listings until at least 200 valid daily bars exist.

### Market regime filter

- NIFTY 50 close above its 200-day EMA.
- NIFTY 50 50-day EMA above its 200-day EMA.
- The 200-day EMA has a positive 20-session slope.
- If any condition fails: no new long positions.

### Stock trend filter

- Adjusted close above 200-day EMA.
- 50-day EMA above 200-day EMA.
- Relative strength versus NIFTY 50 over 63 sessions is positive.
- Optional breadth gate to test, not assume: percentage of NIFTY 100 stocks above their 50-day EMA exceeds 50%.

### Pullback setup

- Price has made a 20-session high within the previous 20 sessions.
- Price pulls back for 2-8 sessions without closing below the 50-day EMA by more than 0.5 ATR(14).
- RSI(5) or another short oscillator enters a predefined pullback zone; parameters must be selected by broad plateau, not best point.
- Volume on the pullback is not materially above the 20-day median unless the recovery bar shows strong demand.

### Confirmation and entry

- A bullish confirmation bar closes above the previous session's high.
- Candlestick names are annotations, not entry triggers. Acceptable annotations include hammer, bullish engulfing, piercing pattern, or strong rejection, but the numerical breakout condition is mandatory.
- Enter next session using a stop-limit or a tested market-on-open implementation. The backtest must model gaps and missed stop-limit fills.
- Cancel an unfilled setup after two sessions or immediately if the regime filter fails.

### Initial stop

- Below the setup's structural swing low minus a volatility buffer of 0.25 ATR(14).
- Reject the trade if the stop distance is below a market-noise floor or so wide that one tradable unit exceeds the risk budget.
- The stop can only tighten; it can never be widened.

### Exit candidates to compare

- Baseline: take partial profit at +1R, trail the remainder below a 10-day low.
- Alternative 1: no partial exit; trail below a 10-day low.
- Alternative 2: exit at +2R or the initial stop.
- Time stop: exit after 15 sessions if the trade has not reached +0.5R.
- Regime exit: close when the stock trend filter breaks, subject to gap/slippage modeling.

No exit becomes final until out-of-sample tests compare expectancy, drawdown, turnover, and tail loss.

## 5. Candidate strategy B: NIFTY options overlay - research only

Options are not simply leveraged direction. The strategy must model direction, time to expiry, implied volatility, spread, slippage, and nonlinear payoff.

Initial research constraints:

- Defined-risk structures only; no naked short options.
- No same-day expiry trades.
- No new trade with fewer than 7 calendar days to expiry in the first experiment.
- Prefer one-lot debit spreads only after the cash/spot signal has demonstrated an edge.
- Maximum premium loss must fit the same account-level risk cap.
- Use executable bid/ask prices, not last traded price.
- Backtests must use historical option-chain data; synthesizing option prices from spot alone is inadequate for production approval.

Strategy B remains disabled until Strategy A's signal quality is understood and reliable option-chain data is available.

## 6. Non-negotiable risk rules

All percentages below are conservative starting proposals and must later be reconciled with the user's actual capital and loss tolerance.

- Risk per trade: 0.25% of current equity during paper trading and initial live validation; hard maximum 0.50% without a formal review.
- Aggregate open risk: maximum 1.00% of equity.
- Correlated cluster risk: maximum 0.50% across stocks from the same sector or highly correlated group.
- Daily stop: stop initiating trades at -1.0R realized plus open loss; never liquidate mechanically solely because of an arbitrary daily profit target.
- Weekly stop: pause new entries at -3R.
- Drawdown throttle: at -5% from equity high, halve risk; at -8%, disable new trades and require a system audit.
- Maximum positions: four initially.
- No averaging down, martingale sizing, revenge trades, discretionary stop widening, or adding to a losing position.
- No trade if estimated reward to the first realistic resistance/target is below 1.5 times initial risk.
- Include brokerage, exchange charges, taxes, spread, slippage, and gap risk in every result.
- A daily profit target is a stopping/discipline rule, not evidence that the market owes a profit. The bot must not force trades to reach it.

Position size formula:

`risk_budget = current_equity * risk_fraction`

`per_unit_risk = abs(planned_entry - initial_stop) + estimated_per_unit_costs`

`quantity = floor(risk_budget / per_unit_risk)`

If quantity is below one tradable unit, skip the trade.

## 7. Behavioral and operating rules

- Before every order, record setup ID, data timestamp, entry, stop, planned exits, quantity, maximum loss, and reason the trade is valid.
- A valid loss is acceptable; a rule violation is not.
- Results are reviewed in fixed batches of at least 30 trades, with 100+ preferred for strategy conclusions.
- After three consecutive losses, size is not increased. Continue only if all trades complied with rules and portfolio limits.
- Parameter changes require a written hypothesis, a new test version, and out-of-sample evidence.
- The LLM may explain and classify; deterministic code must calculate prices, indicators, sizing, limits, and order constraints.
- Missing, stale, inconsistent, or future-leaking data causes a fail-closed `NO_TRADE` decision.

## 8. Validation protocol

### Data quality

- Use adjusted and unadjusted OHLCV correctly; avoid using adjusted prices for actual order simulation.
- Maintain point-in-time universe membership and corporate-action handling.
- Timestamp every feature and enforce that no feature uses information unavailable at decision time.
- Model NSE holidays, current contract specifications, lot sizes, and expiry calendars from versioned reference data.

### Backtest stages

1. Unit-test every indicator and fill rule on hand-calculated examples.
2. Run an exploratory in-sample test only to detect implementation errors and broad parameter plateaus.
3. Lock rules before evaluating untouched out-of-sample periods.
4. Use anchored walk-forward tests across bullish, bearish, sideways, high-volatility, and gap-heavy regimes.
5. Apply realistic costs and adverse slippage stress tests at 1x, 2x, and 3x base assumptions.
6. Bootstrap trade sequences to estimate drawdown and risk of ruin.
7. Compare against buy-and-hold, a simple moving-average baseline, and random-entry controls with identical exits.

### Minimum research gates

- Positive net expectancy after stressed costs in the majority of walk-forward folds.
- Profit factor above 1.20 out of sample; no single fold or symbol supplies a disproportionate share of profit.
- Maximum historical drawdown below the user-approved limit and acceptable under bootstrap stress.
- At least 200 out-of-sample trades for a strategy-level conclusion when data permits.
- Stable performance across a reasonable parameter neighborhood.
- No evidence of look-ahead bias, survivorship bias, duplicate bars, or impossible fills.

### Deployment gates

- Paper trade for at least 60 market sessions and 30 valid signals, whichever is longer.
- Compare live-paper slippage and signal timing with the backtest.
- Shadow mode first: generate signals without orders.
- Human-confirmed one-unit mode next.
- Automated execution is a separate approval milestone and is not implied by successful research.

## 9. Current Indian-market constraints incorporated

As of August 2026, NSE lists NIFTY weekly options and monthly/quarterly contracts with Tuesday expiries, while most other index derivatives are monthly. Contract specifications and permitted lot sizes can change and therefore must be fetched from versioned exchange reference data rather than hard-coded.

SEBI evidence shows that roughly 91% of individual equity-derivatives traders lost money in FY2024-25. This supports the design decision to begin with unleveraged swing signals and keep options disabled during initial validation.

## 10. Later bot architecture

The initial architecture should be a deterministic trading engine surrounded by an LLM research interface, not an LLM that invents orders.

- Data layer: point-in-time market data, corporate actions, instrument master, costs, and calendar.
- Feature engine: deterministic indicators and regime classification.
- Strategy engine: versioned rules returning `LONG_CANDIDATE` or `NO_TRADE`.
- Risk engine: authoritative position sizing, exposure limits, drawdown throttles, and kill switch.
- Backtest engine: event-driven fills, costs, slippage, walk-forward evaluation.
- RAG library: chunked books, research notes, exchange circulars, strategy versions, and trade journal. Citations are required for explanations.
- LangGraph workflow: ingest -> validate data -> calculate features -> generate candidates -> risk check -> human review -> simulated execution -> journal -> post-trade audit.
- Execution adapter: disabled by default; idempotent order IDs, reconciliation, and broker-state verification required before any future use.
- Monitoring: stale-data alarm, broker mismatch, order rejection, abnormal slippage, daily loss limit, and emergency halt.

LangChain is optional plumbing. LangGraph is useful for auditable state transitions. RAG should explain source material and retrieve rules, but it must never override deterministic risk controls.

## 11. Immediate next work

1. Convert the source books into a searchable local evidence index with page metadata.
2. Produce a machine-readable strategy specification and risk-policy schema.
3. Obtain clean historical NSE cash data and a point-in-time constituent history.
4. Implement and test Strategy A only.
5. Report results, weaknesses, and rejected variants before discussing Groww access.

