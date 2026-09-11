# Quote-driven paper simulator (KIW-29)

`kiwit.paper_simulator.PaperSimulator` provides a persistent V2 simulator alongside the existing legacy ledger. It has no broker connector. Initialize it with Decimal capital, a versioned calendar, and frozen FillRules; reopening a database with different configuration rejects.

States are PENDING → OPEN → CLOSED, or PENDING → CANCELLED/REJECTED. Session-end open positions become EXIT_PENDING until a fresh executable quote arrives. Submit requires a matching APPROVE RiskDecision and a valid positive lot quantity. It does not accept an LLM verdict as risk approval. Full current-ledger risk policy/critic orchestration remains integration work.

Orders fill only on an observed quote available at receipt time and no earlier than submission. Future/stale quotes reject; events must be chronological. Buys use ask plus adverse slippage, sells bid minus adverse slippage, rounded adversely to instrument ticks. Fees apply to each fill's turnover. Entry recalculates stop-loss exposure including projected stop slippage and fees and rejects insufficient risk budget or capital. Gap exits use the observed bid/ask, never an invented stop-level fill.

Stops and targets trigger on the executable side of each observed quote. There is no inference from unseen intrabar paths. Fills assume the entire requested quantity is available at the quote; depth/queue/partial-fill modeling is not claimed. Sparse quotes can miss transient touches, and gaps can exceed planned risk. Fees/slippage are research assumptions recorded in persistent metadata, not an exchange fee schedule.

Both long and short positions reserve unlevered entry notional. Portfolio reporting separates cash, closed realized PnL and open marked PnL (including entry fees); equity uses current executable-side marks before hypothetical exit fees/slippage. Marks can remain old until another quote arrives. At session end, pending entries cancel; open positions await a fresh quote for closure. This may leave explicit overnight exposure rather than fabricating a fill.

SQLite transactions serialize updates to the account, orders and event log. Idempotent event IDs return the original result; conflicting retries reject. State/results carry checksums and survive restart. The caller owns deterministic event IDs and chronologically ordered receipt timestamps; same configuration/events yield the same replay state. This local SQLite research engine is not the deployed PostgreSQL paper ledger and does not silently replace it.

Tests cover fills/costs, budget rejection, stop gaps, target exits, session behavior, cancellation, stale/future data, replay equivalence, configuration binding and restart/idempotency. Integration and deployment remain pending.
