# Cross-sectional EMA v2 — immutable research specification

Status: **research only**. This specification is frozen before inspecting results.

## Point-in-time universe

- Source: dated official NSE CM UDiFF final bhavcopies.
- Eligible series: `EQ` only; a security exists only on dates where it appears in that day's archive.
- Minimum history: 120 observed sessions before a signal.
- Liquidity: top 250 securities by trailing 60-session median rupee turnover, shifted one session.
- Price floor: prior close at least INR 20.
- No current index membership or current security list is used.

## Signal and execution

- Long only.
- Entry signal: 20-session EMA crosses above 100-session EMA.
- Regime filter: NIFTY 50 close above its 200-session EMA.
- Exit signal: 20-session EMA crosses below 100-session EMA.
- Signal calculated at close; execution occurs at the next available session open.
- Maximum 20 concurrent positions; equal allocation of available slots.
- Cost and slippage: 10 basis points on each entry and exit; stress levels 0, 5, 20 and 30 bps.
- No leverage, shorting, options or pyramiding.

## Corporate-event hygiene

A trade containing an unexplained absolute close-to-close move above 30% is excluded from inference and counted separately. This avoids treating an unadjusted split/demerger as trading alpha. It does not repair the price series or use future constituents.

## Predeclared promotion gates

All gates must pass:

1. At least 200 completed, corporate-event-clean trades.
2. Profit factor above 1.20 after 10 bps per side.
3. Positive median trade return after costs.
4. At least three of four chronological folds profitable.
5. Maximum portfolio drawdown no worse than -15%.
6. Positive return at 30 bps per side.
7. At least 7 of 9 neighboring EMA parameter combinations profitable.
8. Actual entry timing at or above the 95th percentile of randomized-entry controls using matched holding periods.

Failure of any gate keeps the strategy locked.
