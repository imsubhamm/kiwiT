# kiwiT Strategy Validation v1

Decision: **no strategy promoted to paper trading**

Dataset: 2,658 matched NIFTYBEES/NIFTY 50 sessions, 2015-11-09 through 2026-08-20. Research prices are explicitly adjusted for the 2019 NIFTYBEES split. Execution occurs no earlier than the next session. Primary costs are 10 basis points per side.

## Primary comparison

| Model | Total return | CAGR | Max drawdown | Sharpe | Trades | Exposure | Decision |
|---|---:|---:|---:|---:|---:|---:|---|
| Passive NIFTYBEES | 245.06% | 12.18% | -36.34% | 0.88 | 1 | 99.96% | Benchmark only |
| EMA 50/200 | 144.20% | 8.64% | -17.67% | 0.84 | 19 | 71.67% | Not promoted |
| Donchian 50/20 | 155.38% | 9.09% | -13.36% | 0.98 | 15 | 58.20% | Not promoted |

Both strategies reduce historical drawdown and exposure, but both trail passive absolute return. Neither has enough trades for reliable inference.

## EMA 50/200

Positive evidence:

- Net profit factor is 5.18 at 10 bps per side.
- All nine predeclared neighboring parameter combinations are profitable.
- Return remains positive at 30 bps per side: 126.33% total, 7.87% CAGR.
- The actual entry rule ranks at the 99.8th percentile of 1,000 randomized-entry simulations using the same exit logic.
- Three historical folds are clearly positive. The 2025-2026 fold is effectively flat: +0.015%, with three completed trades and profit factor 0.52.

Failed gates:

- Only 19 trades versus the required 200.
- Maximum drawdown is -17.67% versus the -15% gate.

Bootstrap estimates show a 3.38% modeled probability of loss over a 19-trade resample, but this result is fragile: it assumes independent, identically distributed trades and starts from a very small sample.

## Donchian 50/20

Positive evidence:

- Net profit factor is 11.25 at 10 bps per side.
- All nine parameter neighbors are profitable.
- All four time folds are positive.
- Maximum historical drawdown is -13.36%, inside the -15% gate.
- Return remains positive at 30 bps per side: 140.51% total, 8.48% CAGR.

Failed gates:

- Only 15 trades versus the required 200.
- The entry rule ranks at only the 16.5th percentile of 1,000 randomized-entry controls.

The random-control result is critical. Random entries combined with the same Donchian exit logic produced a median 205.50% return, above the actual strategy's 155.38%. This suggests that the long-holding exit behavior and general upward market path explain more of the result than the breakout entry. Donchian 50/20 is therefore not an acceptable signal candidate despite its attractive headline metrics.

## Promotion gates

| Gate | EMA | Donchian |
|---|---|---|
| At least 200 trades | Fail | Fail |
| Profit factor above 1.20 after costs | Pass | Pass |
| Majority of fixed folds positive | Pass | Pass |
| At least 95th percentile vs randomized entry | Pass | Fail |
| At least 7/9 profitable parameter neighbors | Pass | Pass |
| Maximum drawdown no worse than -15% | Fail | Pass |

Overall promotion requires every gate. Both models remain research baselines.

## What the evidence changes

1. The rejected pullback strategy remains rejected.
2. Donchian is retained as an exit/exposure benchmark, not as a validated entry signal.
3. EMA is the stronger entry hypothesis, but it cannot be approved from 19 trades on one instrument.
4. The next useful experiment is cross-sectional EMA testing across a point-in-time stock universe. This is more valuable than tuning EMA spans on NIFTYBEES.
5. No options or live execution work should begin from these results.

## Reproducibility

The machine-readable suite contains primary results, four fixed folds, five cost levels, nine parameter neighbors per strategy, 1,000 randomized-entry simulations, 10,000 bootstrap simulations, and promotion decisions.

