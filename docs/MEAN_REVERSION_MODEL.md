# Mean reversion model (KIW-18)

`kiwit.ml.mean_reversion` returns LONG/SHORT/NONE and a calibrated `reversion_probability`, complementary failure probability, artifact fingerprint, and reason codes. It neither sizes nor executes trades.

## Setup and target

A setup is eligible when the absolute decision-close distance from EMA21 meets the dataset's `reversion_distance`. Direction is toward that fixed decision EMA: SHORT above it, LONG below it. The existing `strategy-labels-v1` target requires both a smaller absolute distance from that fixed EMA at the horizon and a signed endpoint return, after label cost, meeting the movement threshold. Ineligible rows retain null labels and inference returns NONE.

Features now use `session-features-v2`, adding `ema_21_distance`. Rebuild old datasets and retrain models before using this version; prior feature-version artifacts are rejected. The new distance is computed from the canonical close and decision EMA using the same decimal expression as dataset labeling. Other selected features cover RSI, momentum, EMA slopes, ATR, realized volatility, ADX, and completed five-minute context. VWAP and relative volume remain unselected where index feeds lack volume. Missing selected inputs and unready/stale snapshots block scoring.

## Training

```sh
.venv/bin/python scripts/train_mean_reversion_model.py \
  --dataset data/local/datasets/dataset-FINGERPRINT.json \
  --output data/local/mean_reversion-models \
  --round-trip-cost 0.0002 --round-trip-slippage 0.0004
```

Install the `ml` extra and macOS `libomp` if needed. Cost/slippage defaults are illustrative return fractions. Training requires both outcomes and eligible samples in each chronological, purged split. Temperature calibration uses validation labels only. Test labels cannot affect trees or calibration; validation calibration metrics are in-sample for the calibrator. Fixed seed, CPU execution, native model JSON, runtime versions, label definition and dataset fingerprint are retained in checksum-addressed research artifacts.

## Evaluation

Reports include held-out classification/calibration metrics and baseline comparison by future-labeled regime and date. Future regime labels are used only for diagnostic grouping. A separate strong-trend group uses contemporaneous ADX >= 25. For this group, selected target failures, losing trades, total negative endpoint return and worst net endpoint return measure adverse behavior.

The deterministic baseline takes eligible reversions only when ADX < 20. Both simulations enforce one open position, use decision/horizon closes, and deduct configured round-trip costs and slippage once. Group summaries preserve the selected global trade sequence. These un-compounded unit-return diagnostics do not represent executable fills or portfolio P&L. Worst endpoint loss is not intrabar maximum adverse excursion; the current dataset does not retain the future path needed for that measure.

Artifacts are `RESEARCH_ONLY`. Synthetic tests validate training, inference, persistence, leakage isolation and strong-trend loss accounting. Real-market training, trending/ranging performance assessment and deployment remain pending.
