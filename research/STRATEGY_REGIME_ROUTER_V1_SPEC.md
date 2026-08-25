# Regime Router v1 — immutable research specification

Status: **research only**. Frozen before inspecting results on 2026-08-25.

## Objective

Select one deterministic long-only equity approach from information available at each NSE close. The router is the
strategy under test: its regime classifier, child rules, cash fallback, ranking, sizing, exits and costs are evaluated
together. An LLM may explain outputs but cannot change a regime, signal, quantity or gate.

## Point-in-time universe

- Official dated NSE CM bhavcopies; `EQ` rows existing on each historical date only.
- Minimum 220 observed sessions.
- Prior close at least INR 20.
- Top 250 securities by trailing 60-session median rupee turnover, shifted one session.
- Unexplained close-to-close moves above 30% contaminate a holding; contaminated trades are excluded and counted.
- No present-day constituent list or future security information.

## Regime classifier

Calculated from NIFTY 50 closing data and shifted before the next-session execution:

- `bull_trend`: NIFTY close above EMA(200), EMA(50) above EMA(200), and 20-session return above +2%.
- `range`: NIFTY close above EMA(200), but the bull-trend conditions are not all satisfied.
- `risk_off`: every other state, including insufficient or stale data. Hold cash and admit no new positions.

## Child strategies

### Bull trend — cross-sectional breakout

- Entry: close exceeds the prior 20-session high and is above SMA(50).
- Rank simultaneous entries by 20-session return, strongest first; symbol is the deterministic tie-breaker.
- Exit: close below the prior 10-session low, a close-based loss of 8%, or 40 completed sessions.

### Range — short-horizon pullback

- Entry: two-session RSI below 10, close below SMA(5), and close above SMA(200).
- Rank simultaneous entries by RSI ascending, then symbol.
- Exit: close above SMA(5), a close-based loss of 5%, or 7 completed sessions.

An existing position continues under the child strategy that opened it. Regime changes block new entries but do not
rewrite its frozen exit contract.

## Execution and portfolio accounting

- Signals are calculated after close and executed at the next available session open.
- Maximum 20 concurrent positions; each entry receives 1/20 of current marked equity, rounded down to whole shares.
- No leverage, shorting, derivatives, pyramiding or discretionary overrides.
- Entry and exit cost/slippage: 10 bps per side; stress at 0, 5, 20 and 30 bps per side.
- Cash and every open position are marked to that session's close. Delisted/missing holdings retain the last observable
  mark and cannot create a new signal; a terminal position is liquidated at its last observable close for reporting.

## Predeclared promotion gates

Every gate must pass:

1. Point-in-time universe and next-session execution checks pass.
2. At least 200 completed, corporate-event-clean trades.
3. Profit factor above 1.20 after 10 bps per side.
4. Positive median trade return after costs.
5. At least three of four chronological walk-forward folds have positive net return.
6. Daily marked-to-market portfolio drawdown is no worse than -15%.
7. Portfolio return remains positive at 30 bps per side.
8. At least 7 of 9 predeclared neighbouring configurations have positive return.
9. Actual terminal return is at or above the 95th percentile of 200 seeded randomized-entry controls. Random controls
   preserve the daily regime, eligible universe, number of entry candidates and the same sizing/exit/cost machinery.

Failure or incomplete computation of any gate keeps strategy promotion and scheduled paper execution locked.
