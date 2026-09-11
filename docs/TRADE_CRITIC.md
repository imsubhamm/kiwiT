# Trade Critic (KIW-25)

`kiwit.llm.critic.TradeCritic` reviews a pre-existing serious proposal through the provider-independent gateway. DeepSeek V4 Pro remains the runtime default. It does not discover trades or call a broker.

Strict input includes the instrument/time/snapshot-bound evidence, regime and specialist probabilities, supplied RSI/ATR/EMA distance, historical similarity, structured context output and authoritative proposal. Proposal fields are candidate ID, direction, entry, stop, target, quantity, confidence and thesis. Positive finite prices and directionally valid stop/entry/target geometry are required. Missing required evidence, future/unrelated sources, weak proposals or a context veto return NO_TRADE before a provider call. Producers remain responsible for the truth and point-in-time provenance of supplied summaries.

Under prompt/schema `advisory-roles-v3`, output allows APPROVE/REJECT/UNCERTAIN, confidence, enumerated warnings, reason codes and supplied source references. Extra fields attempting to change proposal parameters fail validation. APPROVE proceeds only with confidence >= 0.7 and no warnings, and means RISK_REVIEW_REQUIRED. REJECT/UNCERTAIN always yield NO_TRADE. All results retain `execution_allowed: false`.

`proposal_for_risk` verifies the review, original proposal and snapshot binding and returns a copy of the caller's unchanged proposal. Altered or rejected reviews return None. This boundary does not itself size or approve risk: mapping to the existing deterministic RiskEngine remains mandatory downstream integration work. Checksums protect against accidental mutation, not malicious forgery.

Gateway audit records preserve prompts, versions, provider/model, input fingerprint, usage and structured output. Critic audit adds the authoritative proposal, snapshot, review fingerprint and handoff result. Audit failure prevents a returned review. Frozen sanitized inputs should be retained by the caller using the logged input fingerprint.

## Frozen scenario evaluation

`benchmark_critic` accepts separate `input` and `outcome` objects. Outcome contains `exit_at` and `net_return`, already net of explicit costs/slippage from an independent replay. It is never sent to the LLM. Repeat reviews measure output consistency; the first review alone determines each scenario's selection and outcome statistics.

Reports include net expectancy, profit factor (null without losses), compounded unit-equity maximum drawdown, losing trades rejected, winning trades falsely rejected, NO_TRADE loss fraction, schema failures, latency, known costs and unknown-cost counts. Accepted scenarios cannot overlap; this diagnostic simulates one unit position at a time and is not portfolio P&L or risk-approved execution. Semantic hallucination rate remains null pending human evidence review. Apply identical frozen scenarios to separately configured runtime and champion gateways; results do not promote a provider automatically.

Tests use mock providers. Actual DeepSeek/champion comparisons, real-data replay outcomes, live pipeline integration and deployment remain pending.
