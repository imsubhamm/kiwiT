# Decision audit and outcome tracking (KIW-13)

The V2 `DecisionJournal` stores original decisions, later paper lifecycle events,
and delayed evaluation labels in separate SQLite tables. Accepted, rejected and
`NO_TRADE` decisions use the same schema. It is an evidence store, not an execution
engine or a model-training job.

## Decision evidence

Each immutable decision includes an explicit identity, timestamp and instrument,
exact canonical candle input, references to persisted raw market batches, complete
feature values/version/readiness/input digest, versioned model scores, candidate,
reason codes, optional LLM evidence and risk checks. LLM evidence, when supplied,
requires prompt text, prompt version, model version and output.

The feature input hash must match the supplied canonical market snapshot. The
historical store verifies that each candle actually exists, matches its persisted
value, has a raw provenance reference, and was available at decision time. Empty
market snapshots can be retained for unavailable-data decisions. Ready features
and a candidate are required for a `TRADE` record; this records a candidate decision,
not authorization to execute it.

Decision identities and event identities are idempotent for identical evidence;
conflicting retries are rejected. Each record carries a checksum verified on read.
Checksums detect payload changes, but this local database is not a cryptographically
signed or externally anchored tamper-proof ledger. Protect and back up the database
and its historical raw-data store together.

## Paper lifecycle and reconstruction

Append later risk and execution records with `append_event`. Execution evidence
requires `mode=PAPER`, status, position identity and a list of fills. Each fill
requires instrument, timestamp, side, price, quantity and fees. Fill numeric values
must be finite and valid, and fill time cannot exceed the event time. Events cannot
precede the decision. This records evidence only; it does not assert that a risk
approval exists, route an order, or independently confirm a producer's fill.

`read(decision_id, as_of=...)` reconstructs the original decision plus all events
and labels visible at that clock. It excludes later execution events and later
labels. `export(as_of=...)` returns chronological JSON-compatible records, including
rejected candidates for counterfactual evaluation. Original features and future
outcomes remain separate fields; downstream training must preserve that boundary.

## Outcome labels

`label_outcomes` evaluates 5, 15, 30 and 60 elapsed-minute horizons. Labels contain
forward close return, optional directional return for an explicit long/buy or
short/sell candidate, movement class, source references and availability timestamp.
The reference is the decision instrument's candle close exactly at decision time.
These are market-movement labels, **not realized paper PnL**, option-return estimates,
or claims that a rejected trade would have filled.

A horizon is labeled only when all minute candles between the decision and target
are present and available, and both endpoints are within the declared session.
Pending, missing, late or out-of-session data is reported explicitly without
inventing a label. A missing/stale reference close also prevents labeling.
Labels become visible at the labeling call's observation clock, which may be later
than the horizon. Existing labels are immutable and are not rewritten by subsequent
runs. The scheduler that invokes this method is a later orchestration integration.

## Credential handling

Sensitive dictionary fields (authorization, tokens, secrets, passwords, API keys,
cookies and credentials) are redacted recursively. Bearer strings, common textual
credential assignments and caller-supplied secret literals are scrubbed before
persistence. Pass actual runtime secret literals through the `secrets` constructor
argument when arbitrary prompt/output text might contain them. Do not pass HTTP
headers, client objects or environment dumps as evidence. Pattern-based redaction
cannot identify every unknown secret embedded in arbitrary prose.

## Feature-engine integration

Use `FeatureDecisionAudit` as the `audit` argument to `FeatureEngine.decide`:

```python
from kiwit.decision_journal import DecisionJournal, FeatureDecisionAudit

journal = DecisionJournal("data/local/decisions.sqlite3", historical_store)
audit = FeatureDecisionAudit(
    journal, decision_id, feature_snapshot, canonical_input_candles,
    candidate=candidate, risk_checks=risk_checks,
)
decision = feature_engine.decide(
    feature_snapshot, model_id=model_version, predict=model_predict, audit=audit,
)
```

The adapter retains the complete model output as well as scores and versions.
Warm-up/invalid inputs produce an audited `NO_TRADE` without invoking the model.
Audit failure prevents the decision method from returning a result. Producers
append risk results and paper execution events when those stages occur.

This integration is available to the new V2 inference path. The deployed legacy
paper desk has not been migrated, and existing trades are not retroactively
reconstructed by this story. No live-order capability is enabled.
