# kiwiT Paper Trading

The paper system is a PostgreSQL ledger driven by deterministic risk decisions and explicit human reviews. It simulates fills from validated quotes and never calls an external broker.

## Account setup

```bash
set -a; source .env; set +a
python scripts/manage_database.py migrate
python scripts/paper_account.py create --account kiwit-paper-main --cash 1000000
python scripts/paper_account.py status --account kiwit-paper-main
python scripts/paper_account.py halt --account kiwit-paper-main --reason "manual safety stop"
python scripts/paper_account.py resume --account kiwit-paper-main --operator "your-name"
```

## Execution invariants

- A proposal must reference a registered immutable strategy and instrument.
- An approving deterministic risk record and approving human review must both exist.
- Account and global system halts block fills.
- Each proposal produces at most one paper order and fill.
- Buys cannot make cash negative; sells cannot exceed the long position.
- Price uses the quote ask/bid plus configurable adverse slippage.
- Brokerage and taxes are recorded separately from fill price.
- Order, fill, cash, position, and daily-ledger updates commit atomically.
- Repeating execution returns the original fill.

The first release is long-only and market-order-only. There is no live broker adapter.

`AccountPaperBroker` binds a PostgreSQL paper account to the LangGraph broker interface. This makes an approved graph execution persist atomically to the paper ledger while leaving the in-memory broker available for isolated tests.
