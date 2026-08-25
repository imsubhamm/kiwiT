# Strategy A v0 Backtest Report

Decision: **REJECT - no deployable edge demonstrated**

Run date: 2026-08-25

## Dataset

- Execution instrument: NIFTYBEES daily OHLCV from dated NSE equity bhavcopy archives.
- Regime instrument: NIFTY 50 daily index OHLC from dated NSE index archives.
- Raw equity coverage: 2,347 sessions, 2015-01-01 through 2024-07-05.
- Raw index coverage: 2,133 sessions, 2015-11-09 through 2024-07-05.
- Matched dates: 2,133; indicators leave an evaluated backtest period of 2015-12-10 through 2024-07-05.
- Duplicate dates: zero. Null values: zero.
- Corporate action: NIFTYBEES 10-for-1 unit split on 2019-12-19, explicitly back-adjusted. The raw archive is retained unchanged.
- Universe: one continuously defined index ETF. This avoids current-constituent survivorship bias; it does not test the planned NIFTY 100 stock-selection layer.

SHA-256 provenance:

- `niftybees.csv`: `441620a43a51458c9a8fc2bcd5e41385f82f23f47416535d0f4af6e11a41a1f4`
- `nifty50.csv`: `3255055e0a67d5eee70f24a268b5f5e2b4f330c46f334911fdabd25811e63e74`
- corporate actions: `3bffca17e854dc6a92ec2820106ce9c49172efd129ec0b527fcceb04cc7cf236`

## Tested rules

- Long only.
- NIFTY 50 above 200 EMA; 50 EMA above 200 EMA; 200 EMA rising over 20 sessions.
- NIFTYBEES above 200 EMA; 50 EMA above 200 EMA.
- Pullback occurs 2-8 sessions after a 20-session peak, remains near the 50 EMA, and RSI(5) is at or below 45.
- Confirmation close exceeds the prior high; entry is next session's open.
- Initial stop is below the recent structural low by 0.25 ATR.
- Risk budget is 0.25% of current equity.
- Half exits at +1R; remaining position trails after partial profit.
- A 15-session time stop applies if progress remains below +0.5R.
- When stop and target are both potentially touched within one daily bar, the engine evaluates the stop first, which is conservative.

## Primary result

Starting capital: INR 1,000,000. Assumed cost and slippage: 10 basis points on each entry and exit.

| Metric | Result |
|---|---:|
| Completed trades | 19 |
| Total return | -0.4532% |
| CAGR | -0.0530% |
| Maximum drawdown | -1.4045% |
| Win rate | 47.37% |
| Profit factor | 0.7275 |
| Mean R per trade | -0.0928R |
| Annualized Sharpe | -0.1670 |

For context only, back-adjusted NIFTYBEES price increased approximately 247.34% over the same prepared span. This is not a risk-matched benchmark and excludes trading costs, but it illustrates the strategy's extreme under-participation.

## Cost sensitivity

| Cost per side | Total return | Max drawdown | Sharpe | Profit factor | Mean R |
|---:|---:|---:|---:|---:|---:|
| 0 bps | -0.0241% | -1.2962% | -0.0069 | 0.9846 | -0.0045R |
| 5 bps | -0.2458% | -1.3515% | -0.0878 | 0.8479 | -0.0487R |
| 10 bps | -0.4532% | -1.4045% | -0.1670 | 0.7275 | -0.0928R |
| 20 bps | -0.8279% | -1.5682% | -0.3181 | 0.5307 | -0.1811R |
| 30 bps | -1.1617% | -1.7190% | -0.4601 | 0.3890 | -0.2693R |

The gross result is already slightly negative, and costs make it progressively worse. This is not a hidden edge defeated only by an aggressive cost assumption.

## Interpretation

1. Nineteen trades are far below the predeclared 200-trade research gate, so statistical confidence is inadequate.
2. The observed expectancy and profit factor fail even before costs.
3. The low drawdown is caused largely by low exposure and cannot compensate for absent returns.
4. Candlestick/pullback confirmation as encoded here is too restrictive for a single index ETF.
5. The result rejects this exact implementation. It does not prove that every multi-timeframe pullback strategy is invalid.
6. Parameter mining around these 19 trades would create a high overfitting risk and is therefore prohibited.

## Data limitations

- Free official index archive availability in this run starts in November 2015.
- The data ends at the July 2024 legacy bhavcopy transition. A separate UDiFF adapter is required for later data.
- The ETF split adjustment is explicit, but a complete official corporate-action ingestion pipeline is still required before multi-stock testing.
- Daily bars cannot reveal the true sequence when stop and target occur within the same bar; the conservative stop-first convention is used.
- Taxes, brokerage, spread, and slippage are represented by a combined basis-point stress assumption rather than a broker-specific charge model.

## Next research decision

Do not tune Strategy A v0. Build Strategy A v1 as a broad, point-in-time stock-universe study only after acquiring dated NIFTY 100 membership and corporate actions. The cleaner alternative is to test a simpler index trend system as a baseline first:

1. NIFTYBEES 50/200 EMA regime with volatility-scaled exposure.
2. A Donchian breakout baseline.
3. The same exits applied to randomized entries to measure whether exit logic alone explains results.
4. Walk-forward and cost stress only after each baseline produces a sufficient trade count.

Options research and broker integration remain disabled.

