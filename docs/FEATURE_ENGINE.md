# Deterministic feature engine (KIW-12)

`kiwit.marketdata.features.FeatureEngine` implements `session-features-v2` over
canonical one-minute candles. Live, historical replay, and offline dataset callers
use the same calculations. The engine resets at each declared session open; it
does not carry recursive indicator state across sessions. This is a new V2
contract, separate from the legacy dashboard's chart heuristics.

## Input and readiness

`from_feed(feed, as_of)` accepts either `GrowwMarketDataService` or
`HistoricalReplay`, using their common Bank Nifty candle interface. It requests
history from the calendar's session open. `compute(result, instrument, as_of)`
provides the pure calculation API for an already time-gated `IngestResult`.

The feed is responsible for observation availability; the engine additionally
excludes candles closing after `as_of`, rejects upstream invalid states, and checks
instrument identity, duplicate conflicts, session alignment and all due gaps.
Historical callers must retain the KIW-11 availability gate rather than pass an
unrestricted full archive directly into `compute`. Naive clocks are rejected.

A snapshot carries its feature version, calendar version, observation clock, hash
of the exact canonical input prefix, values and per-feature unavailable reasons.
Unavailable values are null, never filled with zero. Warm-up permits individual
features to become available at different times. `ready` requires valid market
data and every configured required feature. The default required set is return,
EMA 9/21, RSI 14, ATR 14, ADX 14 and MACD signal; it becomes ready after 34 complete
consecutive minutes. Requirements are configurable by model and recorded with the
snapshot. Volume-dependent features are not required by default because index
candles may lack volume.

## Versioned definitions

Returns, slopes and distances are fractions, not percentages. RSI and ADX use a
0–100 scale; prices/ATR/MACD use instrument price units. Outputs are rounded to ten
decimal places. Calculations use finite Python floats and reject numerical
underflow/overflow that makes input prices nonpositive/nonfinite or output invalid.

| Feature | Definition | Minimum candles |
|---|---|---:|
| `return_1` | Latest close / preceding close − 1 | 2 |
| `momentum_5` | Latest close / close five minutes earlier − 1 | 6 |
| `ema_9`, `ema_21` | SMA seed, then alpha = 2/(period+1) | 9 / 21 |
| `ema_9_slope`, `ema_21_slope` | Latest EMA / previous EMA − 1 | 10 / 22 |
| `ema_spread` | (EMA 9 − EMA 21) / latest close | 21 |
| `rsi_14` | Wilder averages of 14 close gains/losses; flat = 50, gains only = 100 | 15 |
| `macd` | EMA 12 − EMA 26 | 26 |
| `macd_signal`, `macd_histogram` | EMA 9 of MACD; MACD − signal | 34 |
| `atr_14` | Wilder mean of true range, using previous close; first range uses candle two | 15 |
| `realized_volatility_20` | Population standard deviation of 20 log returns; not annualized | 21 |
| `adx_14` | Wilder mean of 14 DX values derived from Wilder directional movements; ties/flat = 0 | 28 |
| `vwap_distance` | Close / session candle-based VWAP − 1; typical price = (H+L+C)/3 | 1 with known positive total volume |
| `relative_volume_20` | Latest volume / mean of preceding 20 volumes | 21 with known volumes and positive baseline |
| `range_fraction` | (Latest high − latest low) / latest close | 1 |
| `compression_20` | Latest range / mean range of preceding 20 candles | 21 with positive baseline |
| `breakout_high_20`, `breakout_low_20` | Close / preceding 20-bar maximum high or minimum low − 1 | 21 |
| `range_5m` | Range / close of latest completed session-aligned five-minute candle | 5 |
| `return_5m` | Return between the latest two completed five-minute closes | 10 |
| `minutes_since_open`, `session_fraction` | Elapsed minutes and fraction of declared session duration at observation clock | 1 |

VWAP is a candle-based approximation, not tick-level exchange VWAP. Unknown
volume yields `MISSING_VOLUME`; an all-zero denominator yields `ZERO_VOLUME`.
Compression with zero historical range yields `ZERO_BASELINE_RANGE`. The current
bar is excluded from relative-volume and range/breakout baselines. Forming
five-minute groups never contribute to higher-timeframe features.

## Model-decision audit boundary

Use `engine.decide(snapshot, model_id=..., predict=..., audit=...)` for V2 inference.
It validates feature/calendar versions and required-feature settings, invokes the
model only for a ready snapshot, and records the model identity, complete feature
snapshot/version/input hash and returned decision through `HashChainAuditLog`.
Unready snapshots produce an audited `NO_TRADE` without invoking the model. Model
output must be JSON-compatible with finite numbers; audit failure prevents the
method from returning a decision. This method has no execution or broker access.

The boundary supports future specialist models; it does not train a model or
reroute the existing deployed LLM/paper-trading desk. Broader model orchestration,
outcome logging and strategy/risk decisions remain separate stories.

## Example

```python
from kiwit.marketdata.features import FeatureEngine

engine = FeatureEngine(calendar)
snapshot = engine.from_feed(replay_or_live_feed, observation_clock)
feature_record = snapshot.to_json_dict()
# Dataset code can retain feature_record, including nulls and unavailable reasons.
# Inference goes through engine.decide(...), which logs the feature contract.
```

Validation includes hand-checkable linear/flat indicator values, warm-up boundaries,
missing/zero volume, future-data exclusion, partial higher-timeframe exclusion,
live/replay parity, gaps/conflicts, invalid numerical input and versioned decision
logs. These tests validate definitions, not predictive performance or profitability.

KIW-18 adds `ema_21_distance` under `session-features-v2`. Existing v1 datasets and models must be rebuilt/retrained; old model feature contracts are rejected rather than silently mixed.
