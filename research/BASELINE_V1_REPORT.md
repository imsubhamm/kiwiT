# kiwiT Baseline v1 Report

Status: research only - not approved for paper or live trading

Dataset: back-adjusted NIFTYBEES daily bars from dated official NSE bhavcopy archives, 2015-01-01 through 2024-07-05. Cost assumption: 10 basis points per side. Signals are evaluated at the close and executed no earlier than the following session.

## Results

| Strategy | Total return | CAGR | Max drawdown | Sharpe | Trades | Win rate | Profit factor | Exposure |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| EMA 50/200 v1.0.1 | 157.54% | 10.46% | -17.67% | 0.98 | 17 | 41.18% | 7.49 | 71.15% |
| Donchian 50/20 v1.0.0 | 128.71% | 9.09% | -13.36% | 0.93 | 15 | 53.33% | 7.36 | 61.44% |

Back-adjusted NIFTYBEES price appreciation over the broad raw period was approximately 247%. This is a contextual price benchmark, not a risk-matched total-return comparison.

## Interpretation

- Both simple baselines materially outperform the rejected pullback implementation.
- Both underperform passive price appreciation in absolute return while spending less time exposed.
- Donchian shows the smaller drawdown in this sample; EMA shows the higher absolute return.
- Profit factors appear unusually strong because there are only 15-17 completed trades. These estimates are fragile and must not be extrapolated.
- Neither strategy meets the predeclared 200-trade research gate.
- The test is a single instrument and a single historical path. It does not establish a generalizable edge.

## Decision

Retain both as engineering and research baselines. Do not promote either to paper trading yet.

Next evidence work:

1. Add the post-July-2024 UDiFF archive adapter.
2. Add walk-forward subperiod reporting and cost stress.
3. Add randomized-entry controls using identical exit logic.
4. Source point-in-time NIFTY 100 membership and complete corporate actions.
5. Run cross-sectional baselines to obtain an adequate sample size.

