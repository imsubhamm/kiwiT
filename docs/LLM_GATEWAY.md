# Strict LLM gateway (KIW-23)

`kiwit.llm.gateway.LLMGateway` exposes asynchronous `analyze_context` and `critique_trade` methods. Install `.[llm]`. GatewayConfig defaults both runtime roles to DeepSeek V4 Pro (`deepseek-v4-pro`). The champion label is GPT-5.6 Sol, not a runtime fallback. Benchmark calls require an explicit provider adapter and its valid API model ID; the label alone is not sent to an API.

Context Analyst receives allowlisted source IDs/text and a supplied quantitative summary, returning LOW/HIGH/UNKNOWN event risk, market context and quant conflict. Trade Critic additionally receives a serious candidate (confidence >= 0.65), direction and thesis, returning APPROVE/REJECT/UNCERTAIN. Neither role receives broker access, quantity/stop/target mutation fields or tools. Every response has `execution_allowed: false`; APPROVE is advisory and cannot create a trade.

Inputs and outputs use strict Pydantic schemas with extra fields forbidden, bounded lengths, finite confidence, enumerated reasons and source references restricted to supplied evidence. Duplicate JSON keys, malformed/empty output, unsupported citations, tool calls and truncated replies fail closed. Source text is explicitly treated as untrusted data in separate versioned prompts. The gateway cannot guarantee that an LLM interprets text correctly; deterministic downstream uncertainty and risk gates remain mandatory.

The included DeepSeek adapter posts only to a fixed HTTPS chat-completions endpoint, rejects redirects, requests JSON mode, bounds response size and exposes no tools. JSON mode does not replace local schema validation. Other providers implement the same async Adapter contract without changing strategy/risk code.

Each provider attempt has an enforced async deadline (default 20 seconds), with at most two attempts. Only transient transport/timeout/429/5xx failures retry, with a short bounded backoff; invalid output does not retry. Custom adapters must be cancellation-cooperative. Audit records contain requested/resolved model, provider, prompt/version/schema, input fingerprint, attempt latency and token counts. Cost is an estimate using explicit configured input/output prices; unknown usage/prices remain null, never fabricated zero. Failed attempts may have unknown provider billing. Benchmark calls do not inherit runtime prices.

Audit writes are mandatory before returning. Raw provider prose, credentials, and exception messages are not logged. Explicit secret literals and secret-shaped fields are redacted before sending allowlisted input. Keep frozen sanitized inputs separately by fingerprint for replay; audit logs identify them but do not contain source text. The gateway holds only its provider adapter; do not inject broker capabilities or credentials.

`benchmark` accepts frozen `{role, input}` scenarios, returns scenario fingerprints/results and uses an independent provider/model configuration. It preserves runtime settings and redaction. No paid provider calls or actual champion benchmark were made during implementation; tests use deterministic fake providers and an HTTP mock.

No new gateway is deployed or connected to the legacy engine yet. The Context Analyst and Critic producer stories, runtime credential provisioning, real frozen-scenario evaluation and deterministic downstream mappings remain pending.

Provider reference: [DeepSeek JSON Output](https://api-docs.deepseek.com/guides/json_mode/) and [Chat Completions API](https://api-docs.deepseek.com/api/create-chat-completion/). DeepSeek documents `deepseek-v4-pro` and warns JSON mode may return empty content; this gateway blocks such replies.

KIW-24 upgrades the context output contract under `advisory-roles-v2`: required fields are `event_risk`, `market_context`, `quant_conflict`, `confidence`, `reason_codes`, and `source_ids`. Old risk-only responses are rejected.

KIW-25 upgrades the critic contract under `advisory-roles-v3`, requiring an enumerated `warnings` list in addition to verdict, confidence, reason codes and source IDs. Old responses without warnings are rejected.
