# Point-in-time supervised datasets (KIW-14)

`kiwit.ml.datasets.DatasetBuilder` generates `supervised-dataset-v1` artifacts
from recorded canonical history using the existing session replay and feature
engine. It prepares datasets; it does not train models or establish trading
profitability. No production dataset or external backfill is created automatically.

## Feature and label separation

Each row contains decision time, split, a `session-features-v2` snapshot, separate
labels, label horizon endpoint, the cutoff used to verify label availability and
an outcome-input digest. The feature snapshot includes its own input digest and
required-feature contract. Future candle values enter labels only.

Replay queries restrict features to observations actually available at the sample
time. Missing/warm-up/invalid features exclude the sample. If a dataset starts
mid-session, earlier candles from that session can warm the indicators, but samples
before the requested start are omitted. Missing values are not imputed.

Labels use a complete sequence of subsequent one-minute candles, beginning at the
sample time and ending at its configured horizon. Missing bars, late availability
or a horizon extending outside the session exclude the sample. There is no
next-day bridging or synthetic price filling.

## Initial label specification

`strategy-labels-v1` is an explicit research definition, not a learned classifier
or a claim about true market regimes. Default horizon: 15 minutes. Defaults are
movement threshold 0.001, high-volatility range 0.01, reversion distance 0.002,
and cost fraction 0. All thresholds are fractions, not percentages. Supply a
versioned `LabelSpec` with appropriate research thresholds/cost assumptions.

Let `r` be horizon close / decision close − 1 and `c` the configured cost fraction.
The regime label uses the future high-low range divided by decision close.

| Target | Eligibility and outcome |
|---|---|
| Regime | HIGH_VOLATILITY if future range meets its threshold; otherwise UPTREND/DOWNTREND if `r` crosses the positive/negative movement threshold; otherwise RANGE. High volatility takes precedence. |
| Trend success | Direction is the sign of EMA 9–21 spread at decision time. Success when signed `r − c` meets the movement threshold. Zero/unknown spread is ineligible. |
| Breakout success | Direction is positive for a close above the prior 20-bar high, negative below the prior 20-bar low. Success uses signed return less cost. Without a breakout, target is null, not failure. |
| Mean-reversion success | Eligible when close-to-EMA21 distance meets the configured threshold. Direction opposes that distance. Success requires sufficient signed return less cost and a horizon close closer to the **decision-time** EMA21. |

Binary success/failure labels are 1/0; null means no eligible setup. Side fields
are +1/−1/0 and preserve direction. `forward_return` is retained separately.
These endpoint-based labels do not model stops, intrahorizon fills, slippage,
path-dependent exits or derivative payouts. Cost fraction is a research assumption,
not an automatically determined broker fee estimate. Strategy models must use only
feature fields as predictors, never these outcome/eligibility fields from labels.

## Chronological splitting and leakage checks

`TimeSplits` defines exclusive train, validation and test end timestamps. Training
starts at the requested dataset start, validation at train end, and test at
validation end. All clocks must be timezone-aware, boundaries strictly increasing,
and the build cutoff at or after test end. There is no random row split.

A sample is purged when its target is at or beyond its split end. Label data must
also be available **strictly before that split end**, even when the dataset is
built much later. The `label_checked_as_of` field records this verification cutoff;
it is not a claim about the exact receipt time of each label candle.

`walk_forward` creates rolling chronological train/validation/test windows from
explicit positive durations and a step. Every fold applies the same purging and
availability rules. Windows may overlap across folds if configured that way;
consumers must not treat overlapping test observations as independent results.
No scaling, resampling, feature selection or fitting is done here. Later training
must fit such transforms on training data only.

## Reports and reproducibility

Reports contain counts per split, excluded-sample reasons, per-target class counts,
ineligible counts, majority fractions and warnings for empty splits, absent
eligible targets, and majority fractions above 0.8. Leakage checks cover unique
sample times, feature-clock alignment, positive label horizons, and horizon and
availability containment within splits. Empty datasets explicitly warn rather
than being represented as useful training data.

Artifacts include dataset/feature/label versions, exact label configuration,
calendar version **and dated sessions**, instrument, boundaries, required features,
cutoff, data digests and report. A content fingerprint names the JSON artifact.
Identical source state/configuration yields identical bytes; changing source data
or label parameters changes the fingerprint. Re-saving identical data is
idempotent. Mutated artifacts with stale fingerprints are rejected.

## Build command

```sh
.venv/bin/python scripts/build_training_dataset.py \
  --db data/local/history.sqlite3 --calendar data/local/calendar.json \
  --symbol BANKNIFTY --segment INDEX --series INDEX \
  --start '2026-08-01T00:00:00+05:30' \
  --train-end '2026-08-15T00:00:00+05:30' \
  --validation-end '2026-08-22T00:00:00+05:30' \
  --test-end '2026-08-29T00:00:00+05:30' \
  --as-of '2026-08-30T00:00:00+05:30'
```

Dates are illustrative. Use a complete calendar and stored observations with
trustworthy historical availability. Optional `--labels path.json` loads LabelSpec
fields; `--output` sets the artifact directory. The command prints the artifact
path, fingerprint and quality report. A dataset generated from today's downloads
cannot claim that those records were available in an earlier period without
separate trustworthy publication metadata.
