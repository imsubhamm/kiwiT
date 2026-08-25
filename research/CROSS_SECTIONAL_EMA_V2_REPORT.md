# Cross-sectional EMA v2 evidence report

Decision: **NOT APPROVED**

The specification was frozen before inspecting results. No parameter was changed after the first run.

## Dataset

- 3,021,372 official NSE `EQ` observations from dated daily bhavcopies.
- 3,359 symbols observed from 2020-01-01 through 2026-08-20.
- The daily universe is point-in-time: securities exist only when present in that date's archive.
- Liquidity selection uses the prior 60-session median turnover and never uses a current constituent list.
- 39 trades containing an unexplained move above 30% were excluded as possible unadjusted corporate events.

## Primary result

Assumption: 20 concurrent equal-sized slots, 10 basis points per side.

| Metric | Result |
|---|---:|
| Capacity-admitted trades | 342 |
| Capacity-rejected signals | 1,446 |
| Total realized-slot return | 198.70% |
| Profit factor | 2.50 |
| Median trade return | -3.37% |
| Win rate | 28.07% |
| Realized-equity maximum drawdown | -14.67% |
| Total return at 30 bps per side | 179.12% |

The result is highly positively skewed: a minority of long trend winners pays for many losing trades. That is compatible with trend following, but it fails the predeclared positive-median gate.

## Gate decision

| Gate | Result |
|---|---|
| At least 200 trades | Pass |
| Profit factor above 1.20 | Pass |
| Positive median trade | **Fail** |
| At least 3/4 profitable folds | Pass |
| Maximum drawdown no worse than -15% | Pass, preliminary |
| Positive at 30 bps per side | Pass |
| At least 7/9 profitable neighbors | Pass |
| At least 95th percentile vs randomized entries | **Not completed** |

Every gate is mandatory, therefore promotion is prohibited.

## Important limitation

The current portfolio curve books returns at exits. It does not yet mark every open position to market each session. Consequently, the reported drawdown is preliminary and may be understated. Randomized-entry controls are also outstanding. Neither test is necessary to reject the candidate because the median gate already fails, but both are required before any future promotion decision.

## Next decision

Do not tune EMA spans around this result. Preserve v2 as a rejected, reproducible baseline. The next candidate must be a separately specified hypothesis with daily mark-to-market accounting and randomized controls implemented before results are inspected.
