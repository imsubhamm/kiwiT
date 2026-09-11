# Model and strategy evaluation reporting (KIW-31)

Generate a standalone HTML report and companion JSON from one or more KIW-30 backtest artifacts or explicitly labelled normalized paper runs:

```sh
.venv/bin/python -m kiwit.evaluation_dashboard run.json --output evaluation.html
.venv/bin/python -m kiwit.evaluation_dashboard run.json --mode backtest --model MODEL_FINGERPRINT --config CONFIG_HASH --output filtered.html
```

`--prompt` filters exact prompt versions. Filters apply to entire runs; paper and backtest runs remain separate even when multiple runs match. Duplicate run IDs are rejected. No API route or deployed dashboard has been added.

## Metrics

Reports show closed-trade net P&L, win rate, expectancy, profit factor, sampled equity drawdown, submitted orders, exact NO_TRADE counts and all decisions with no submitted order. Submitted orders are not necessarily fills. Breakdowns use strategy, regime and decision-hour in Asia/Kolkata. Closed P&L is attributed to decision time; open positions are excluded from trade statistics but remain reflected in the source equity curve.

Calibration bins and Brier score use net-positive closed trades as an explicitly labelled outcome proxy. This is selection-biased and is not validation of a specialist's original training target. Rejection outcomes and LLM verdict outcomes require independently supplied counterfactual labels. They do not establish a causal benefit from the LLM. Cost totals cover known observations only, with coverage counts; missing prices are never silently interpreted as measured zero cost.

Degradation compares expectancy in the chronological earlier and recent halves of decision events. The report exposes both sample sizes and marks significance as unestablished. Sparse or profitable samples never authorize promotion. Comparisons across model versions require comparable frozen datasets and evaluation periods outside this reporting function.

## Normalized input contract

A run contains `run_id`, explicit `mode` (`paper` or `backtest`), `models` (artifact fingerprint list), `prompt_version`, `config_version`, positive `initial_equity`, `equity_curve` (`at`, `equity`) and `rows`. Use one run per fixed model/prompt/config combination. Timestamps must be timezone-aware and strictly increasing within each series.

Each row represents one decision: required `at` and `status`; optional `strategy`, `regime`, `confidence` in [0,1], and `net_pnl` for a closed executed trade only. Optional `llm_verdict` is APPROVE, REJECT or UNCERTAIN; `candidate_outcome` is the independently measured signed counterfactual net outcome under a fixed exit/cost policy. Optional `latency_ms` and `llm_cost_usd` are nonnegative per-decision totals. Use null or omit unavailable values. Never duplicate a trade's realized P&L across rows. Outcome labels are offline evaluation inputs, not inputs to decision generation.

KIW-30 import verifies the source checksum, binds configuration by hash and model artifacts by fingerprint, and joins closed simulator orders to selected candidates. Its prompt version is `UNRECORDED`: the source does not currently retain enough evidence to infer prompt attribution, counterfactual outcomes or full latency/cost. Supply normalized, provenance-backed exports for those metrics. This adapter intentionally cannot import a paper run as a backtest implicitly.

The HTML contains escaped text and no external scripts or network dependencies. The JSON is suitable for later dashboard integration. Synthetic unit tests cover arithmetic, degradation, coverage, filtering, mode separation, malformed data and source checksum verification; no real-market performance claims are made.
