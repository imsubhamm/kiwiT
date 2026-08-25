# kiwiT Architecture

kiwiT is a production-oriented research and paper-trading platform. It is not an autonomous LLM trader.

## Safety boundary

- LLM/RAG components may retrieve, classify, summarize, and explain.
- Strategy calculations, position sizing, portfolio limits, workflow transitions, and execution gates are deterministic.
- Live execution is disabled in the default configuration and no live broker adapter exists.
- The archived Strategy A v0 is registered as rejected and cannot emit a trade proposal.

## Package boundaries

- `kiwit.domain`: immutable financial and workflow contracts using decimal arithmetic.
- `kiwit.config`: validated TOML configuration and environment controls.
- `kiwit.strategy`: versioned strategy protocol and registry.
- `kiwit.risk`: fail-closed position and portfolio risk decisions.
- `kiwit.workflow`: explicit paper-trading state transitions.
- `kiwit.execution`: idempotent in-memory paper fills.
- `kiwit.audit`: append-only, hash-chained JSONL audit events.
- `kiwit.database`: PostgreSQL configuration, health checks, locked checksum-verified migrations, and transaction boundary.
- `kiwit.rag`: deterministic ingestion, page-aware chunks, stable hashes, and citation-bearing retrieval.
- `kiwit.intelligence`: evidence and safety boundary for model prompts; it has no execution capability.
- `kiwit.langgraph_workflow`: durable, interruptible orchestration around deterministic validation, risk, and paper execution.
- `kiwit.checkpointing`: PostgreSQL-backed LangGraph checkpoint lifecycle with strict deserialization.
- `kiwit.paper_trading`: atomic PostgreSQL paper ledger, simulated costs, positions, cash, and idempotent fills.
- `kiwit.api`: authenticated, paper-only operator API and same-origin dashboard.
- `tradingkiwi`: existing research data and backtesting code, retained during migration.

## Initial trust model

The risk engine trusts only validated domain objects. It does not consume natural-language instructions. The workflow requires a positive deterministic risk decision followed by explicit human approval before a paper fill. Duplicate proposal execution is rejected.

## Planned services

The kernel will later be wrapped by API, scheduler, and monitoring services. PostgreSQL persistence, market-data ingestion, and local RAG foundations now exist; those boundaries depend on the kernel rather than embed financial rules in API handlers or model prompts.
