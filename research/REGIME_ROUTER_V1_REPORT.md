# Regime Router v1 validation report

Decision: **rejected — do not promote or start paper trading**.

The frozen router was evaluated on 3,021,372 dated official NSE observations covering 3,359 symbols from 2020-01-01
through 2026-08-20. It used a point-in-time top-250 liquidity universe, next-session-open fills, 10 bps costs per side,
20 equal slots and daily mark-to-market accounting.

## Primary result

| Metric | Result |
|---|---:|
| Clean completed trades | 2,670 |
| Total return | +66.69% |
| Profit factor | 1.316 |
| Median trade after costs | +0.647% |
| Win rate | 57.15% |
| Daily marked-to-market maximum drawdown | **−32.78%** |
| Bull-breakout trades | 685 |
| Range-pullback trades | 1,985 |
| Corporate-event-contaminated exclusions | 10 |

At 30 bps per side the portfolio remained positive by 2.71%. All nine neighbouring configurations were profitable.
The four chronological fold returns were +48.69%, −22.63%, +60.12% and −10.59%.

Two hundred seeded randomized-entry portfolios used the same daily regimes, eligible universe, signal counts, sizing,
exits, capacity and costs. The actual router ranked at the 74.5th percentile; the required threshold was the 95th.

## Gate decision

Passed: point-in-time construction, trade count, profit factor, positive median trade, 30-bps stress and parameter
neighbourhood. Failed: three-of-four positive folds, maximum drawdown no worse than −15%, and 95th-percentile random
control. Because every gate is mandatory, `regime_router@1.0.0` has no promotion or approval record. Groww execution
remains disabled and unattended paper scheduling remains locked.

Do not tune v1 after observing these results. A future v2 must begin with a separately frozen hypothesis aimed at
reducing portfolio drawdown and adding entry information that materially exceeds randomized security selection.
