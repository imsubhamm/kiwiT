# Trend opportunity model (KIW-16)

`kiwit.ml.trend` trains a binary XGBoost specialist on a versioned KIW-14 dataset. Direction comes from the contemporaneous EMA spread: positive is LONG, negative is SHORT. The learned probability estimates the dataset's `trend_success` target for that direction and horizon. Zero-spread samples are ineligible and retain null targets.

Prediction returns LONG, SHORT, or NONE, opportunity probability, model fingerprint, calibration metadata, and reason codes. Missing, stale, unready, or incompatible features return NONE. Default acceptance requires probability >= 0.6 and success-versus-failure probability margin >= 0.1. The module does not size or execute trades.

## Training

Install the project's `ml` optional dependencies; macOS XGBoost also requires the OpenMP runtime (`libomp`). From the repository root:

```sh
.venv/bin/python scripts/train_trend_model.py \
  --dataset data/local/datasets/dataset-FINGERPRINT.json \
  --output data/local/trend-models \
  --round-trip-cost 0.0002 --round-trip-slippage 0.0004
```

These cost defaults are illustrative return fractions, not a broker fee schedule. Supply assumptions appropriate to the instrument and evaluation period. Each split must contain eligible samples; training requires both successes and failures.

Training uses the chronological, purged dataset splits, fixed seed, CPU histogram trees, and one thread. Only validation labels fit temperature calibration. Validation calibration metrics are therefore in-sample for the calibrator; test metrics remain held out. The checksum-addressed artifact contains native model JSON, dataset fingerprint, feature/label/calendar versions, parameters, runtime versions, and evaluation. Reproducibility is tested within the same runtime environment.

## Evaluation and limitations

Reports include classification, Brier score, log loss, reliability bins, signal coverage, and observed success rate, separately for train, validation, and test. Results are grouped by session date and dataset regime. Regime groups use future outcome labels for diagnostic evaluation only; those labels never enter prediction.

The deterministic baseline takes the current EMA direction when ADX >= 20. Both strategies enter at the decision close and exit at the label horizon close, allowing only one open position across the split. Group summaries filter these globally selected trades, preserving overlap exclusion. Gross signed endpoint returns have configured round-trip cost and slippage subtracted once. Label-spec costs define the training target separately and are not deducted again from gross evaluation returns.

Return sums are unweighted, un-compounded research diagnostics, not portfolio P&L. This simulation does not model executable fills, sizing, margin, intrabar exits, or liquidity. Artifacts remain `RESEARCH_ONLY`. Tests use explicitly synthetic market histories; real-market training, validation, and deployment are pending.
