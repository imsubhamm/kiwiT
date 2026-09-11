# Unified historical backtesting (KIW-30)

`kiwit.unified_backtest.PaperDecisionCore.process` is the shared event entry point for historical replay and a future paper service. `run_backtest(core, start=..., end=..., tape=...)` feeds chronological minute events from the trading calendar and returns a fingerprinted report.

## Shared flow

Each event first applies its execution quote to existing simulator orders, then reads only historical candles available at that event time. It computes the standard feature snapshot, scores the four registered specialists, and runs the uncertainty and meta-decision policies. Model fingerprints stay fixed throughout a run. Missing training cutoffs, or training/calibration ending after the event, block model eligibility.

Eligible candidates use deterministic sizing and the current simulated portfolio. A frozen critic review must match the event time, feature snapshot and exact calculated proposal. Independent risk checks run afterward; a quantity change requires a new review. Approved orders enter the durable paper simulator and can fill only on a subsequent supplied quote. The simulator applies configured slippage, fees, capital and stop-risk checks.

## Inputs and use

Construct a fresh core with a `HistoricalStore`, explicit `TradingCalendar`, canonical and execution instruments, `ModelRegistry`, `DecisionJournal`, fresh `PaperSimulator`, `DeterministicRiskPolicy`, `SizingRules` and an audit sink exposing `append(kind, details)`. Risk and sizing budgets should agree. Emergency disable remains enabled by default and must be deliberately configured for a paper experiment.

The tape is a dictionary keyed by exact UTC ISO minute timestamps. Each value contains an optional domain `Quote` and policy `checks`, including the snapshot-bound context/critic checks and full proposal-bound `critic_review`. Missing evidence blocks decisions; missing quotes are never manufactured. Quotes between minute events are not replayed by this adapter. Frozen advisory results must have been produced using only evidence available at the corresponding event, with outcomes kept separate.

For paper integration, call `core.process(at=..., quote=..., checks=...)` in strictly increasing time order. The deployed paper loop is not wired to this entry point yet.

## Reports and limits

Reports retain model bindings, policy configuration, decision evidence, the simulator ledger, equity curve, closed-trade net P&L, wins/losses, expectancy, maximum sampled equity drawdown and strategy/regime breakdowns. Identical data, configuration, model artifacts and frozen advisory inputs reproduce the report fingerprint. Open positions remain visible when a replay ends before closure; closed-trade metrics exclude them.

The orchestrator requires a fresh run and one serialized caller. Although simulator transactions survive restart, orchestration state recovery and atomicity across the separate audit and execution stores are not implemented. Fill assumptions remain those of the paper simulator, including full fills without depth simulation. Calendar provenance and real-market data validation remain operational prerequisites.

Tests exercise the full feature-to-fill path with deterministic model/provider fixtures, repeated report fingerprints, paper/replay parity and future-data rejection. They do not establish investment performance, validate real trained models, or authorize live trading.
