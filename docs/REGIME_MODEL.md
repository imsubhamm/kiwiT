# Market Regime baseline (KIW-15)

`kiwit.ml.regime` trains a CPU XGBoost multiclass probability model from a verified
KIW-14 dataset. Its outputs are TREND_UP, TREND_DOWN, RANGE and HIGH_VOLATILITY,
with UNCERTAIN as an inference abstention, not a fifth supervised class. Dataset
UPTREND/DOWNTREND targets map to TREND_UP/TREND_DOWN outputs. These are predictions
of the versioned future-horizon regime proxy defined in KIW-14, not a verified
classification of an objective current regime.

## Training and evaluation boundaries

Only the training partition fits trees. Default parameters are 40 boosting rounds,
maximum depth 3, learning rate 0.1, seed 7, a single CPU thread, histogram trees,
and full row/column sampling. There is no test-driven tuning or early stopping.
All four regime classes must be represented in training, and train, validation
and test must each contain samples. Dataset fingerprints, versions, timestamps,
split/horizon containment and selected finite feature inputs are revalidated.

The fixed feature list contains return, momentum, EMA spread/slope, RSI, MACD
histogram, ATR, realized volatility, ADX, range, five-minute return and session
fraction. Optional volume features are excluded from this baseline. Selected
missing/nonfinite features reject training rather than being implicitly imputed.
Feature selection and scaling are not fitted on validation or test data.

Validation data selects a probability temperature from the fixed grid
0.5, 0.75, 1, 1.5, 2, 3 by multiclass log loss. The probability transform is
softmax(log(probability)/temperature), with numerical clipping. The validation
post-calibration metrics describe the calibration fit partition and are not an
unbiased test estimate. Test metrics are computed only after the model and
temperature are fixed.

Each split reports raw and calibrated accuracy, four-class macro F1, per-class F1,
confusion matrix, log loss, multiclass Brier score, ten-bin expected calibration
error and reliability bins. Absent-class F1 is zero. Selective coverage and
accepted-only accuracy describe the uncertainty threshold. These are classifier
metrics, not PnL or evidence of a tradable strategy.

## Rule baseline and diagnostics

The rule comparator marks high volatility when current realized volatility exceeds
the training partition's 90th percentile. Otherwise, ADX at least 20 plus the sign
of EMA spread gives trend up/down; remaining observations are range. It is a
specified research comparator, not an exchange standard or trading recommendation.
Rule metrics use hard one-hot scores; their log loss is numerically clipped and
should not be confused with calibrated probabilistic forecasts.

Diagnostics include normalized split-gain feature importance, training feature
ranges, per-split class support, and whether held-out macro F1 beats the rules.
Gain importance is descriptive, not causal, and may favor features with more split
opportunities. Beating this comparator does not automatically promote a model.
Artifacts always remain `RESEARCH_ONLY`.

## Uncertainty and inference

Default inference requires maximum calibrated probability at least 0.6 and a gap
of at least 0.1 between the two highest probabilities. Otherwise it returns
UNCERTAIN with scores and reason codes. Invalid/warm-up snapshots or mismatched
feature versions return UNCERTAIN without model scores. Thresholds are explicit
configuration, not optimized on test data.

`RegimeModel.predict(feature_snapshot)` returns structured probabilities, regime,
reason codes, feature version, model version and artifact fingerprint. It does not
produce a trade action or access a broker. OOD/drift detection is not implemented;
confidence thresholds alone do not guarantee that unfamiliar data is recognized.

## Artifacts and walk-forward use

Each JSON artifact contains the native XGBoost JSON model, fixed feature/class
order, training parameters, uncertainty policy, calibration temperature, evaluation
report, dataset fingerprint and versions, label configuration and split boundaries,
feature diagnostics, and runtime versions/platform. The artifact is content-hashed
and validated on load. Serialization uses JSON, not Python pickle. Identical builds
are tested within the same runtime; byte identity across different native runtimes
or platforms is not guaranteed.

`evaluate_walk_forward` trains and calibrates each fold independently and records
its test report and model fingerprint. Test windows must be chronological and
non-overlapping. With an output directory, every fold model is saved. The summary
macro F1 is the unweighted mean across folds, not a pooled independent-sample score.

## Setup and commands

```sh
.venv/bin/python -m pip install -e '.[ml]'
# macOS also requires the OpenMP runtime if not already installed:
brew install libomp

.venv/bin/python scripts/train_regime_model.py \
  --dataset data/local/datasets/dataset-REPLACE_WITH_FINGERPRINT.json \
  --output data/local/regime-models
```

Repeat `--dataset` in chronological order for walk-forward folds. These paths are
placeholders; the command does not manufacture training data. In this development
session, XGBoost 3.3.0 and its macOS OpenMP dependency were installed. Integration
tests train against explicitly synthetic four-regime fixtures and verify repeatable
artifacts, loading/inference, calibration, uncertainty, split validation and that
changing test labels cannot change the fitted model or temperature. No real-market
V2 dataset was present, so no production model or real-market performance claim
has been produced.

XGBoost API/serialization reference:
[Official Python API](https://xgboost.readthedocs.io/en/stable/python/python_api.html).
