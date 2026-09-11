# kiwiT Market-Data Pipeline

## Sources

- NSE legacy capital-market bhavcopy through 2024-07-05.
- NSE UDiFF CM final bhavcopy from 2024-07-08.
- NSE daily index snapshot for NIFTY 50.
- Versioned corporate-action reference data.

The UDiFF adapter reads named ISO-tag fields and rejects files missing required columns. The official transition is documented by NSE Circular 62424 and the NSE Forms & Formats page.

## Zones

- `data/market/raw`: immutable downloaded archives, excluded from version control.
- `data/market/manifests`: URL, date, retrieval time, hash, byte size, and status.
- `data/market/normalized`: source-specific normalized bars with per-file hashes.
- `data/market/quarantine`: validation reports for failed ranges.
- `data/market/published`: research-ready unified datasets and their manifest.

## Publication rules

A range is not published when it has duplicate dates, non-positive prices, invalid OHLC relationships, negative volume, unexplained moves above 40%, or an equity trading date with no matching index date. Index-only dates are warnings because an ETF can occasionally lack a valid row.

Corporate actions are never applied to the execution view. The research view is explicitly adjusted and stored separately. Raw downloads are never rewritten.

## Commands

Download and validate the UDiFF period:

```bash
python scripts/ingest_market_data.py --start 2024-07-08 --end 2026-08-20 --workers 16
```

Build the unified execution, research, and index datasets:

```bash
python scripts/build_unified_market_dataset.py
```

## Known limitations

- The free official data sources do not provide one convenient, complete point-in-time NIFTY 100 membership history. The membership model and overlap validation exist, but the dataset is not yet populated.
- Corporate-action ingestion is currently a versioned local reference for NIFTYBEES, not an automated all-equity feed.
- A formal NSE holiday calendar adapter is still needed. Successful matched exchange dates currently define the observed calendar.
- Exact broker-specific transaction costs are outside this pipeline and belong in the execution-cost service.


## KiwiT V2 canonical contract (KIW-9)

`kiwit.marketdata.canonical` defines the `market-v1` contract. Instrument identity
contains exchange, segment, series and exchange symbol, with optional ISIN.
Candle open/close boundaries are timezone-aware UTC; the exchange calendar uses
Asia/Kolkata. Prices serialize as decimal strings, and unknown volume stays null.
Quotes distinguish exchange time from receipt time; receipt time never refreshes
an old exchange observation. Unsupported versions and naive contract timestamps
are rejected. The Groww adapter alone explicitly interprets naive provider ISO
timestamps as IST and supports epoch seconds/milliseconds.

Live chart parsing and historical chart replay use `groww_candles`. The NSE
archive pipeline additionally publishes `.market-v1.jsonl` files beside existing
CSV exports. These files round-trip through `Candle.from_json_dict`. Daily archive
boundaries describe a session, not the time a final archive became available;
point-in-time training must separately account for publication availability.
Legacy CSV consumers remain compatible.

`ordered_candles` sorts by instrument and interval, collapses identical records,
and rejects conflicting duplicates rather than choosing an arrival-order winner.
`assess_market` takes an explicit observation clock, instrument, versioned calendar
and freshness policy. It returns `invalid_market_state`, `trade_allowed`, and
stable reason codes for absent/gapped/misaligned/stale/forming data, conflicting
duplicates, and required missing or stale quotes. The candle coverage requirement
starts at session open. Input history must include the complete session so far.

`TradingCalendar` requires explicitly supplied dated sessions. Missing dates fail
closed; holidays can be omitted and special weekend/short sessions supplied.
There is no bundled authoritative holiday feed. Adapter compatibility defaults
use regular 09:15–15:30 session boundaries to describe source records; those defaults
do not establish trading eligibility. V2 eligibility assessment always requires
an explicit calendar. Existing paper execution retains its existing checks; wiring
the V2 state assessment into the new decision engine belongs to the decision and
uncertainty-policy stories. No broker execution permissions change in this story.

## Groww V2 data service (KIW-10)

`GrowwMarketDataService.from_settings(settings, store, calendar)` composes the
existing authentication-aware broker client with a read retry transport. It
exposes canonical quotes and completed Bank Nifty candles, with no order methods.
Callers must call `result.require_valid()` before using observations for strategy
evaluation. Invalid results contain stable reason codes, never raw upstream errors.
Receipt time is sampled after I/O, so slow calls cannot conceal stale market data.
Candle requests must cover the current session from its opening boundary for the
complete-session quality policy to pass. Historical replay remains a separate
consumer; old history is deliberately stale for live evaluation.

The transport spaces requests by at least 250ms by default and retries transient
GET failures (429, selected 5xx, network errors) up to three attempts with bounded
exponential backoff. Rate spacing/backoff are configurable; the defaults are local
policy, not a claim about Groww account quotas. Authentication failures remain
with the existing client's single token-refresh path. Token POST calls are never
retried by this transport. Polling calls use fresh HTTP requests after an error;
there is no WebSocket subscription/reconnection state in this REST service.

`ObservationStore` persists canonical observations transactionally in SQLite,
with content hashes preventing identical retry duplicates. It stores only normalized
market fields, never tokens or raw responses. Persistence failure invalidates the
result. The SQLite store is a local ingestion implementation, not a PostgreSQL
migration or the full historical replay service. Integration with the new V2
orchestrator/strategy engine follows their respective stories; the existing deployed
paper desk has not been switched to this service.

## Historical storage and replay (KIW-11)

The indexed historical store, required availability timestamps, chronological
replay API, quality reports, optional live-ingestion hookup, and CLI are documented
in [Historical market store and replay](HISTORICAL_REPLAY.md). Both live and replay
feeds return the same canonical candles and `IngestResult` interface.

## Deterministic features (KIW-12)

[Feature-engine definitions and usage](FEATURE_ENGINE.md) document the shared
`session-features-v2` calculations, warm-up/missing-input states, live/replay input
contract, and feature-version logging at the V2 model-decision boundary.

## Decision evidence and outcomes (KIW-13)

[Decision journal](DECISION_JOURNAL.md) links model decisions to raw/canonical
market inputs, records rejected candidates and paper lifecycle evidence, redacts
credentials, and keeps delayed 5/15/30/60-minute labels separate from original
feature inputs.

## Supervised datasets (KIW-14)

[Training datasets](TRAINING_DATASETS.md) documents point-in-time feature rows,
initial strategy label definitions, chronological purging, rolling walk-forward
folds, deterministic artifacts, and class-balance/leakage reports.

## Market Regime baseline (KIW-15)

[Regime model](REGIME_MODEL.md) documents the XGBoost baseline, rule comparison,
held-out calibration evaluation, uncertainty policy, reproducible artifacts and
walk-forward workflow. Real-market model evaluation remains pending suitable data.

## Trend opportunity scoring

See [Trend opportunity model](TREND_MODEL.md) for KIW-16 training, calibrated directional scoring, and cost-aware baseline evaluation. This remains research-only pending real-market validation.

## Breakout scoring

See [Breakout model](BREAKOUT_MODEL.md) for KIW-17 continuation/failure probabilities, versioned targets, and held-out false-breakout diagnostics.

## Mean reversion scoring

See [Mean reversion model](MEAN_REVERSION_MODEL.md) for KIW-18 reversal probabilities, baseline comparison and strong-trend loss diagnostics. Its EMA distance input introduces feature contract v2; rebuild datasets and retrain prior model artifacts.

## Shared ML framework

See [ML framework](ML_FRAMEWORK.md) for KIW-19 version-bound training, local registry activation/rollback, and fail-closed common inference.

## Strategy candidates

See [Strategy candidate generator](STRATEGY_CANDIDATES.md) for KIW-20 regime compatibility, conflict retention, confidence gates and deterministic cooldown handling.

## Explicit uncertainty policy

See [NO_TRADE policy](UNCERTAINTY_POLICY.md) for KIW-21 versioned abstention rules and mandatory decision evidence.

## Meta decision engine

See [Meta Decision Engine](META_DECISION_ENGINE.md) for KIW-22 deterministic eligibility, ranking, audit evidence and the mandatory downstream risk boundary.

## LLM gateway

See [LLM gateway](LLM_GATEWAY.md) for KIW-23 separated advisory roles, strict schemas, provider adapters, bounded calls and benchmark isolation.

## Market Context Analyst

See [Context Analyst](CONTEXT_ANALYST.md) for KIW-24 point-in-time supplied evidence, structured context risk and frozen-scenario evaluation.

## Trade Critic

See [Trade Critic](TRADE_CRITIC.md) for KIW-25 proposal-bound advisory verdicts and outcome-blind benchmarking.

## Champion/challenger evaluation

See [LLM evaluation](LLM_EVALUATION.md) for KIW-26 frozen corpus/report artifacts, identical provider inputs, leakage checks and promotion prerequisites.

## Mandatory risk policy

See [Risk policy](RISK_POLICY.md) for KIW-27 independent hard limits, paper-only decisions and audit requirements.

## Deterministic sizing

See [Position sizing](POSITION_SIZING.md) for KIW-28 versioned numeric stop/target rules, lot/tick rounding and risk/capital caps.

## Durable paper simulation

See [Paper simulator](PAPER_SIMULATOR.md) for KIW-29 transactional quote-driven fills, lifecycle and portfolio accounting.

## Unified historical replay

See [Unified backtest](UNIFIED_BACKTEST.md) for KIW-30 shared feature/model/decision/risk orchestration, quote-driven paper execution and reproducible reports.

## Model and strategy evaluation

See [Evaluation dashboard](EVALUATION_DASHBOARD.md) for KIW-31 version-filtered HTML/JSON reporting, degradation, calibration and evidence coverage.

## Controlled model learning

See [Model promotion](MODEL_PROMOTION.md) for KIW-32 frozen retraining plans, held-out comparison gates, manual activation and recorded rollback.

## Market-session lifecycle

See [Session orchestrator](SESSION_ORCHESTRATOR.md) for KIW-33 durable lifecycle scheduling, readiness gates, restart/manual RUN behavior and remaining EC2 integration.
