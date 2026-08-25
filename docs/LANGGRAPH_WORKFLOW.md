# kiwiT LangGraph Workflow

The graph orchestrates deterministic components; it does not delegate trading decisions to an LLM.

```text
START -> validate_data -> assess_risk -> human_review (interrupt)
                                      -> reject -> END
human_review --approve--> execute_paper -> END
human_review --reject---> reject -> END
```

`human_review` uses LangGraph's dynamic `interrupt()` and must be resumed with the same `thread_id` using `Command(resume={...})`. Production uses `PostgresSaver`; in-memory checkpoints are tests only. Node side effects use proposal-derived idempotency keys because an interrupted or failed node may execute again.

The graph accepts only JSON-safe state. Decimal values and timestamps are serialized as strings, which makes checkpoints portable and prevents binary floating-point money calculations.

## Safety properties

- Missing, stale, future, or mismatched quote data rejects before risk assessment.
- Only the deterministic `RiskEngine` calculates quantity.
- A positive risk decision always pauses for human approval.
- Approval authorizes paper execution only.
- Rejection never calls the broker.
- Paper fills and audit events are idempotent across node retries.
- `LANGGRAPH_STRICT_MSGPACK=true` restricts checkpoint deserialization.
- Live execution remains absent.

## Production checkpoint setup

Install `.[workflow]`, load `KIWIT_DATABASE_URL`, and call `postgres_checkpointer(setup=True)` once to install LangGraph's checkpoint tables. Subsequent application startup should use `setup=False`. Every run needs a globally unique, stable thread ID—normally the proposal UUID.
