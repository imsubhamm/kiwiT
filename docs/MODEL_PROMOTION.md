# Controlled learning workflow (KIW-32)

`kiwit.ml.promotion.PromotionWorkflow` extends the local SQLite model registry with immutable learning plans, dataset snapshots, evaluation evidence and manual promotion/rollback records. It does not deploy models or enable live trading.

## Sequence

1. Adopt an existing research champion in the registry. Initial bootstrap still uses `ModelRegistry`; a governed workflow refuses to bootstrap from training metrics.
2. Call `plan(TrainingRun(...), dataset, folds=[...], gates=PromotionGates(...))`. The dataset checksum is verified and the entire dataset, training configuration, champion fingerprint, evaluation windows/data fingerprints and predefined gates are stored before training. Each fold supplies `start`, `end` and `data_fingerprint`. Windows must be ordered, nonoverlapping and after training/calibration for both models.
3. Call `retrain(job_id)`. It invokes the existing deterministic trainer and immutable artifact registry. Retry returns the registered challenger; the champion remains active.
4. Call `compare(job_id, evaluator)`. The trusted evaluator is a Python callable `(kind, model_fingerprint, fold)` returning `{'data_fingerprint': ..., 'report': ...}`. Use an isolated KIW-30 core/registry/simulator for each model and frozen historical tape. The report must cover the exact window endpoints, carry a valid checksum and bind the requested model. Other specialists, risk/configuration and simulator metadata must match between champion and challenger. Evaluation evidence is stored once; gates cannot be retuned within that job.
5. Review the evidence, then explicitly call `approve_and_promote(job_id, actor=..., reason=..., at=...)`. Every fold must pass sample, strict expectancy improvement, drawdown ceiling and drawdown regression gates. Approval must follow evaluation, identify an operator and include a rationale. If the champion changed since planning, promotion fails. Activation and its approval record commit in one transaction.
6. Call `restore(promotion_id, actor=..., reason=..., at=...)` to restore that promotion's previous champion atomically. Stale rollback requests cannot replace a different current champion.

Default gates require at least 30 closed trades per model per fold, strictly improved expectancy, drawdown no greater than 10% and no drawdown regression. These are initial configurable controls, not proof of statistical validity. A successful comparison remains `MANUAL_APPROVAL_REQUIRED` and explicitly marks significance as unestablished.

## Boundaries

The evaluator adapter is trusted execution code responsible for running the bound tape and producing genuine report metrics. Checksums establish consistency, not authenticity of externally manufactured evidence. The workflow does not download historical datasets, fabricate comparisons or make LLM/provider calls. Fixed challengers can be assessed over multiple chronological held-out windows; rolling retraining requires separate plans per training cutoff.

Operator identity is recorded, not authenticated by this local library. Production adoption must put these calls behind authenticated operator controls and restrict direct SQLite/research-registry access. `PromotionWorkflow.activate` and `.rollback` reject bypasses, but the older `ModelRegistry` remains available for research compatibility. No production caller has been migrated automatically.

Tests use actual small synthetic model training and fixture evaluation reports to verify lifecycle, gates, bindings and rollback. No real challenger has been promoted and no empirical model improvement is claimed.
