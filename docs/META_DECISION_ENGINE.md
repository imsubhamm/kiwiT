# Baseline Meta Decision Engine (KIW-22)

`kiwit.ml.meta.MetaDecisionEngine.evaluate` is the common entry point for replay and paper evaluation. Both supply the same snapshot, version-bound model scores, explicit clock, context/liquidity/critic checks, market evidence, journal and previous-candidate history. No wall-clock reads or broker operations occur in the engine.

The engine always runs KIW-21's mandatory uncertainty policy first. Eligible candidates are ranked by confidence descending, then configured strategy priority, then candidate ID. The top candidate receives TRADE eligibility; all blocked states receive NO_TRADE. This is eligibility only: every result has `execution_allowed: false` and `risk_approval_required: true`. `candidate_for_risk_review` returns a candidate only from an intact positive result. It does not create an order or grant risk approval.

Rules use `meta-rules-v1`; full threshold configuration, policy decision ID, snapshot fingerprint, ranking, selected candidate and reason codes are recorded. The policy record retains all specialist/regime/context evidence. A separate journal event marks `META_ELIGIBILITY_NOT_RISK_APPROVAL` and preserves the meta result. Either logging failure prevents a decision return. Checksums identify accidental mutation, not hostile forgery.

A future supervised meta-model or RL ranker implements the version-bound CandidateRanker interface. It receives copies of already eligible candidates and can return only a permutation of their IDs. It cannot invent candidates, modify their source evidence, bypass uncertainty, size trades or authorize execution. Invalid output or ranker exceptions produce logged NO_TRADE. Custom implementations must supply their own determinism and reproducibility evidence; the baseline is deterministic.

Context or critic outputs that are not yet available retain the KIW-21 fail-closed behavior. No synthetic confidence or PASS is filled in. The existing portfolio RiskEngine remains a required subsequent integration step once a priced proposal is built; the meta engine does not fabricate entry, stop or cost data for it.

Tests verify replay/paper equivalence, deterministic tie-breaking, rejection before ranking, constrained extension behavior and mandatory audit writes. This implementation is local; the deployed legacy engine has not been rerouted through it, and this story does not activate live trading.
