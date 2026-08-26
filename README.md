# kiwiT / TradingKIWI

Indian-market research and experimental paper-trading platform. The Bank Nifty options desk combines read-only Groww data, multi-timeframe chart evidence, an OpenAI analyst and deterministic paper-execution controls. Earlier NSE archive research and cash-paper workflows remain separate.

Groww integration is read-only; live order execution is disabled. See [Bank Nifty paper desk](docs/banknifty-ai-paper.md) and [chart-analysis rules and limitations](docs/CHART_ANALYSIS.md). Pattern detections are not evidence of profitability, and no automatic model training is implemented.

The new `kiwit` package is the production-oriented kernel. The older `tradingkiwi` package contains the first research data and backtesting implementation while migration proceeds.

## Commands

Use the bundled Python runtime available in Codex:

```bash
python scripts/download_nse_data.py --start 2015-01-01 --end 2024-07-05
python scripts/run_backtest.py
python -m unittest discover -s tests
```

Run the kiwiT configuration safety check:

```bash
PYTHONPATH=src python -m kiwit.cli doctor
```

Run and persist the research baselines:

```bash
python scripts/run_baselines.py
```

See `docs/LOCAL_RESEARCH_RUNBOOK.md` for the complete local workflow.

Market-data architecture and publication rules are documented in `docs/MARKET_DATA.md`.

Run the strategy validation suite:

```bash
python scripts/run_strategy_research.py
```

Prepare the PostgreSQL production database:

```bash
docker compose up -d postgres
pip install -e '.[production]'
python scripts/manage_database.py migrate
python scripts/manage_database.py health
```

See `docs/DATABASE.md` for credentials, migration, backup, and recovery requirements.

Build and query the citation-preserving local knowledge index:

```bash
python scripts/build_knowledge_index.py
python scripts/build_knowledge_index.py --query "risk per trade and position sizing"
```

See `docs/INTELLIGENCE.md` for the RAG trust boundary and model-evaluation gate.

The human-in-the-loop LangGraph workflow is documented in `docs/LANGGRAPH_WORKFLOW.md`. It supports durable resume through PostgreSQL checkpoints and paper execution only.

Create and inspect the persistent paper account with `scripts/paper_account.py`. See `docs/PAPER_TRADING.md` for ledger and execution invariants.

Run the secured API and operator dashboard with `scripts/run_api.py`. See `docs/API_DASHBOARD.md` for local setup and deployment security requirements.

EC2 bootstrap and merge-to-main deployment are defined under `deploy/` and `.github/workflows/`; see `docs/DEPLOYMENT.md`.

Operational endpoints, metrics, alert thresholds, incident response, and rollback are documented in `docs/MONITORING_OPERATIONS.md`.

Application, host, CI, secret-rotation, and infrastructure security requirements are documented in `docs/SECURITY.md`.

The read-only Groww reconciliation adapter and the mandatory gate before order mutation are documented in `docs/BROKER_INTEGRATION.md`.

The July 2024 endpoint transition is intentionally the default cutoff for this first archival dataset. New-format UDiFF ingestion will be a separate adapter.
