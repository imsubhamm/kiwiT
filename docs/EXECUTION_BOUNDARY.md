# Execution boundary (KIW-35)

The unified replay/paper core submits through `PaperExecutionBoundary`. Its trusted authorization method requires current, matching meta-decision, proposal-bound critic and mandatory risk evidence. It signs a versioned instruction binding the full instrument, prices, quantity, proposal identity and evidence IDs. Submission rejects changed payloads, another process's signatures and timestamps outside the approved event. Simulator order IDs retain durable retry idempotency.

`KIWIT_EXECUTION_MODE` defaults to PAPER and is checked during construction, authorization and submission. LIVE, unknown and empty values fail. `DisabledGrowwLiveAdapter` cannot be constructed; existing Groww mutation methods remain disabled. No broker client or credentials are supplied to this boundary or the LLM layer. The older in-memory PaperBroker also now checks the risk decision's approval state.

This is an application capability boundary, not a sandbox for hostile Python code. Trusted orchestration owns the signer and simulator; do not expose either to model tools. Approval evidence hashes detect mutation, not provenance from an adversary able to invoke trusted Python internals. Reauthorization is required after process restart. Independent operational halts and portfolio/session risk checks remain responsibilities of the calling trusted orchestration. Existing legacy PostgreSQL and Bank Nifty flows retain their own paper gates; this change wires the V2 unified flow only.

Tests cover the genuine synthetic decision flow, signature tampering, expiry, retry idempotency, restart rejection and live-mode rejection. No live orders or deployment are performed by this build.
