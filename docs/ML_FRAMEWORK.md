# Versioned ML framework (KIW-19)

`kiwit.ml.framework` provides a shared training entry point and SQLite registry for regime, trend, breakout, and mean-reversion models. The registry stores immutable model JSON and training configuration, bound to model, feature, dataset, calendar, and framework versions. Checksums are integrity checks, not signatures against a malicious database owner.

## Reproducible training

Create a JSON config with `kind` (`regime`, `trend`, `breakout`, or `mean_reversion`), the exact `dataset_fingerprint`, and `parameters` such as `{"rounds":40,"seed":7}`. Optional version fields default to the current supported versions and are persisted explicitly. Use the versioned dataset file and matching runtime recorded in the model artifact to reproduce training. The supported calibration hook is `validation_temperature`; unknown hooks are rejected, and the specialist trainers fit calibration only on validation data.

```sh
.venv/bin/python scripts/ml_registry.py --registry data/local/models.sqlite3 train \
  --config training-run.json --dataset data/local/datasets/dataset-FINGERPRINT.json
.venv/bin/python scripts/ml_registry.py --registry data/local/models.sqlite3 activate \
  --kind trend --fingerprint MODEL_FINGERPRINT
.venv/bin/python scripts/ml_registry.py --registry data/local/models.sqlite3 rollback --kind trend
```

Training registers without activation. Activation validates the native model and stored bindings inside a transaction. Each model kind has an independent activation pointer. Rollback moves that pointer to the preceding activation; it leaves other kinds untouched. Repeated rollback walks older activations and rejects when none remain. Artifact and activation records remain stored. SQLite serializes activation/rollback writes.

## Common inference

Call `registry.score(kind, feature_snapshot)`. The response identifies framework/model/feature versions, dataset fingerprint, model fingerprint, and activation ID, along with the specialist prediction. Missing active models, corrupted data, unavailable native dependencies, incompatible feature/calendar versions, invalid probabilities, and unready snapshots return `BLOCKED`, direction NONE, and no prediction. A valid score may still have direction NONE or regime UNCERTAIN due to confidence thresholds; `SCORED` is not execution authorization.

All loaded models are revalidated for each score, so there is no stale model cache after rollback. Selected-model probabilities retain their calibration metadata. This interface is ready for service integration but is not connected to the deployed engine. Local activation does not deploy anything or change an artifact's `RESEARCH_ONLY` status. Real-market validation and production promotion remain separate pending work.
