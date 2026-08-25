# kiwiT Intelligence and RAG

kiwiT intelligence is an evidence-grounded research assistant, not a trading authority. It retrieves books and project records, preserves source/page citations, and prepares bounded context for a model. It has no broker adapter and cannot approve risk or calculate authoritative order quantities.

## Build and query the local index

```bash
python scripts/build_knowledge_index.py
python scripts/build_knowledge_index.py --query "position sizing and maximum loss"
python scripts/evaluate_rag.py
```

After applying PostgreSQL migrations, synchronize the content-addressed corpus:

```bash
set -a; source .env; set +a
python scripts/sync_knowledge_postgres.py
```

The synchronization uses conflict-safe inserts and can be rerun without duplicating sources or chunks.

The default local index is `data/local/kiwit_knowledge.sqlite3`. Ingestion is content-addressed and idempotent. Changing a source creates a new source version rather than overwriting evidence.

The production PostgreSQL representation is in `migrations/003_knowledge.sql`. It uses generated `tsvector` data and a GIN index. Model embeddings remain optional until semantic retrieval demonstrates measurable improvement on a versioned evaluation set.

## Trust boundary

- Retrieved documents are treated as untrusted content, not instructions.
- Every material model claim must cite a supplied chunk ID.
- No matching evidence must produce an explicit insufficiency response.
- Strategy calculations, market prices, risk sizing, approvals, and execution remain deterministic.
- The intelligence object deliberately exposes no order-execution method or broker tool.
- Exchange rules and circulars need effective dates; current rules must never be inferred from old books.

## Model integration gate

The first frozen retrieval set is `config/rag_evaluation.json`; `evaluate_rag.py` enforces recall-at-k before changes are accepted. Before connecting an LLM provider, extend it to cover contradictory sources, prompt injection, missing evidence, and time-sensitive exchange rules. Measure retrieval recall, citation correctness, groundedness, abstention, latency, and cost. A model/provider change is a versioned release and must pass the same evaluation set.
