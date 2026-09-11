# Breakout model (KIW-17)

`kiwit.ml.breakout` trains a research-only XGBoost model on KIW-14 datasets. It returns calibrated `continuation_probability`, complementary `failure_probability`, LONG/SHORT/NONE, model fingerprint, calibration thresholds, and reason codes. It does not size or execute trades.

## Explicit label contract

The versioned `strategy-labels-v1` dataset defines an eligible breakout as a decision close strictly above the preceding 20 candles' high (LONG), or below their low (SHORT). Equality or an inside-range close is ineligible and has a null target. The decision candle is excluded from the boundary window.

Continuation means signed close-to-horizon return minus the label's cost fraction is at least its movement threshold. Failure is the complement among eligible samples. Thus “false breakout” here means failure to meet that endpoint continuation target; it does not specifically require an intrabar return inside the old range. Horizon, thresholds, cost, label version, and this definition are retained in the model artifact.

## Inputs and inference

Inputs include momentum, EMA/ADX alignment, ATR and realized volatility, range compression, distances to prior high/low boundaries, and completed five-minute features. Relative volume is not selected in this version because it is unavailable for some index feeds. Missing selected inputs block scoring rather than being filled with zero. No breakout, stale/unready features, or low confidence yields NONE. Probability defaults require >= 0.6 and a success/failure margin >= 0.1 for an actionable direction.

## Training and evaluation

```sh
.venv/bin/python scripts/train_breakout_model.py \
  --dataset data/local/datasets/dataset-FINGERPRINT.json \
  --output data/local/breakout-models \
  --round-trip-cost 0.0002 --round-trip-slippage 0.0004
```

Use the project's `ml` optional dependencies (and `libomp` on macOS). Costs shown are illustrative return fractions. Eligible samples are required in all chronological splits, with successes and failures in training. Seeded CPU histogram training and validation-only temperature calibration use the dataset's purged boundaries. Test targets cannot change trees or calibration. Validation calibration metrics are in-sample for the calibrator.

Held-out reports include reliability bins, Brier/log loss, classification metrics, and failure counts/rates for all eligible and selected breakouts, with filtered failures. Reports are separated by regime and session date. Future regime labels are diagnostic groupings only, never inference inputs.

A deterministic boundary-break-plus-ADX>=20 rule provides a comparison. Endpoint strategy diagnostics exclude overlapping positions and subtract round-trip costs/slippage once from gross returns. Group summaries preserve the globally selected trades. Return sums are un-compounded research diagnostics without sizing, executable-fill modeling, or portfolio accounting.

Checksum-addressed artifacts retain native model JSON, runtime versions, parameters, feature and dataset contracts, calibration, and evaluation. Reproducibility is verified within the same environment. Tests exercise synthetic histories through dataset building and training. Real-market training and validation remain pending; artifacts are `RESEARCH_ONLY` and are not deployed.
