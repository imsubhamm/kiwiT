# Explicit NO_TRADE policy (KIW-21)

`evaluate_and_record` in `kiwit.ml.uncertainty` combines the candidate generator with explicit abstention rules. It requires a feature snapshot, bound model scores, decision time, market candles, a DecisionJournal and externally supplied checks. Missing checks block; they never default to approval.

The immutable `UncertaintyConfig` and `uncertainty-policy-v1` are stored with each decision. Defaults: maximum age 90 seconds; realized volatility <= 0.01; spread fraction <= 0.002; depth >= 1 unit; context confidence >= 0.7; entry session fraction [0.02, 0.95). These are configurable research defaults. Providers must normalize depth to the configured instrument units and supply timestamped checks bound to the exact feature snapshot. Context confidence must be explicit and critic status exactly PASS.

Reasons cover insufficient/stale/future data, weak signals, model disagreement, unsupported regimes, missing specialist evidence, excess/unknown volatility, unknown/insufficient liquidity, context/critic uncertainty, cooldown and entry-window restrictions. Candidate conflicts and original model evidence remain in the journal. This policy is deliberately stricter than candidate generation: an unavailable specialist blocks even when another specialist produced a candidate.

NO_TRADE returns no actionable candidates and `execution_allowed: false`. A pass returns CONTINUE_TO_RISK, still with `execution_allowed: false`. `candidates_for_risk` accepts only an intact, explicit positive policy result; missing, modified, unknown or NO_TRADE states return no candidates. Checksums detect accidental changes, not malicious forgery. No broker or order function is called.

Journal writing is mandatory before returning either state; storage failure raises, preventing downstream permission. NO_TRADE records preserve rejected states for the existing delayed-outcome labeling flow. Positive states use the new journal action REVIEW, distinct from TRADE. Callers must not catch audit failures and substitute an actionable default.

The policy is implemented and tested locally. It has not yet been wired into the deployed legacy trading engine. Context/critic producers, liquidity normalization, risk review and real-market threshold validation remain integration work.
