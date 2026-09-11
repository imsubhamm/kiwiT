# Market Context Analyst (KIW-24)

`kiwit.llm.context.ContextAnalyst` wraps the provider-independent gateway. DeepSeek V4 Pro remains the configured runtime default; use a separate gateway with another adapter/model to compare champions without changing runtime settings.

Inputs are strict structured quant summaries, instrument, decision timestamp, snapshot fingerprint, and supplied evidence. Source records identify instrument (or MARKET), publication/availability timestamps, category and text. Categories cover news, central banks, earnings, macro, regulatory, geopolitical and calendar evidence. Future, timezone-naive, duplicate, unrelated or absent evidence blocks before a provider call. Scheduled future events may be described in evidence already published and available at decision time. An optional historical-similarity summary is supplied context, not a retrieval or indicator calculation performed by the LLM.

The context prompt/schema is now `advisory-roles-v2`, requiring event_risk (LOW/HIGH/UNKNOWN), market_context (POSITIVE/NEGATIVE/NEUTRAL/UNKNOWN), quant_conflict (YES/NO/UNKNOWN), confidence, enumerated reason codes and supplied source IDs. Old risk-only responses fail schema validation.

The analyst returns BLOCK unless event risk is LOW, quant conflict NO, sentiment known and confidence >= 0.7. The permissive state is NO_ADDITIONAL_VETO, never trade approval. `context_confidence_for_policy` validates that state and snapshot binding for the KIW-21 context check. It does not set critic PASS, change a candidate, invent an order, size a position or bypass risk. Caller-provided evidence and snapshot fingerprints must come from the trusted point-in-time data pipeline; the gateway does not authenticate source truthfulness.

Gateway and analyst records retain prompt/provider/requested and resolved model/schema versions, input fingerprints, structured output, timestamps, effect and usage. Audit failure prevents return. Preserve sanitized frozen inputs separately for replay.

`evaluate_scenarios` takes frozen `{input, event_risk, quant_conflict}` gold scenarios and repeats each evaluation. Reports include risk recall, false alarms, conflict accuracy, schema compliance, consistency, latency, known cost/unknown cost counts and fraction blocked. High-risk UNKNOWN results count as recall misses; blocking is reported separately. Semantic hallucination rate and realized trading impact remain null because they require human evidence review and an independently joined replay/outcome evaluation. These are not inferred from schema validity or invented from mock data.

Tests use provider fixtures, not paid calls. Real DeepSeek/champion scenario comparisons, source-feed integration, live policy wiring and deployment remain pending. This module has no broker capabilities and does not issue BUY/SELL recommendations.
