# Bank Nifty chart evidence v1

This extends the experimental options paper desk, not the rejected cash router.
No model migration, live order permission, fine-tuning, daily-report service or
profitability approval is included.

## Data and point-in-time rules

- Read-only Groww `/v1/historical/candles`, `NSE-BANKNIFTY`, 1-minute interval.
- Fetch today's session from 09:15, plus a 14-calendar-day historical window.
- Historical context uses the last **five complete observed regular sessions**;
  it is not labelled a calendar week. Actual session dates are displayed.
- Filter regular-session starts 09:15–15:29 IST; timestamps mean candle opens.
  Only candles whose close time is at or before analysis time are accepted.
- Validate finite positive OHLC, minute alignment, order, and duplicate conflicts.
  Identical duplicates are deduplicated; gaps are never forward-filled.
- A historical session must contain all 375 minute candles. Partial sessions in
  the selected context are flagged and block entries; fewer than five complete
  sessions also blocks entries. An absent day is not automatically a verified
  holiday: an exchange calendar is still a separate requirement.
- 5m/15m candles are aggregated at 09:15-aligned boundaries. Partial or gapped
  buckets are excluded. Current-day gaps block entries. First 15m context is
  available after 09:30. Latest minute close must be within 120 seconds.
- Five-session context is cached in the daily session's `chart_cache` JSONB and
  reused across worker restarts. Only complete contexts are cached. Day/version
  changes invalidate it. Mid-session provider revisions to past days are not
  automatically incorporated; the cached context is the session's fixed baseline.

## Deterministic calculations

1m uses up to 100 current-session candles. 5m/15m each use up to 100 candles,
including prior sessions. No synthetic overnight candles are inserted.

- EMA9/21: simple-average seed followed by alpha `2/(period+1)`.
- ATR14: arithmetic mean of the last 14 true ranges, including overnight gaps.
- RSI14: ratio of the last 14 summed positive/negative changes (simple-window,
  **not Wilder smoothing**); flat window is 50.
- Regime: EMA separation <= 0.25 ATR is `range`; otherwise EMA9 above/below
  EMA21 is `uptrend`/`downtrend`. Insufficient data is explicitly labelled.
- Context: five-session OHLC and return (first open to last close), previous-day
  high/low/close, opening gap and completed first 15-minute opening range.

## Explicit 5m setup definitions

- Opening-range / previous-day breakout: previous close on one side of the
  level and latest close crosses it. Invalidation is that level.
- Breakout/retest: previous close beyond the preceding 20-bar high/low; latest
  candle touches within 0.1 ATR of the level and closes beyond it. Invalidation
  is the breakout level.
- EMA pullback: matching trend regime; latest candle touches EMA9 and closes
  beyond it with matching candle direction. Invalidation is candle low/high.
- Engulfing: opposite-direction prior body fully engulfed by the latest body.
  Invalidation is the two-candle extreme. No overnight two-candle pattern.
- Hammer/shooting star: nonzero body; rejection wick >=2x body and opposite wick
  <=body. Invalidation is the candle extreme. These are shape detections, not
  claims that a reversal is statistically likely.
- Range rejection: range regime, latest candle touches the preceding 20-bar
  extreme within 0.1 ATR and closes back inside with matching direction.

Evidence includes rule ID, timeframe, observation timestamp, direction, strategy,
level, observed close and invalidation. Setups older than five minutes or already
invalidated by the latest 1m close are removed. No fitted confidence percentages.

## AI and execution boundary

The unchanged model receives a compact summary, not all chart bars. Its instruction
requires citing the detected setup and invalidation and considering timeframe
conflicts. BUY is rejected unless a fresh, ready analysis includes evidence matching
CE/bullish or PE/bearish and the model's momentum/reversal selection. HOLD/EXIT
remain available; deterministic exit monitoring runs before analysis or model calls.

The chart invalidation is **underlying-index context**, not a replacement for the
existing option-premium stop, profit target or sizing controls. This release leaves
those existing controls unchanged. Missing context blocks entries, not exit monitoring.

Session `chart_analysis` stores current full evidence and bounded chart bars.
`banknifty_ai_calls.snapshot` stores the compact evidence actually sent to the AI;
paper-entry audit events store matching setup evidence. The API excludes the larger
historical cache from status responses. The UI shows 1m/5m/15m candles, prior-day and
opening-range lines, indicators, detected setups, timestamps and stale/error labels.
All external/model text is rendered as text, never interpreted as HTML.

## Validation and remaining limits

Read-only replay on 2026-08-26 at 15:00 IST successfully produced context from
Aug 19, 20, 21, 24 and 25 and valid current-session indicators. No active setup
was detected at that timestamp. No AI call or paper trade was made in this check.

Tests cover future exclusion, duplicate conflicts, malformed OHLC, bucket boundaries,
missing history, session gaps, indicator warmup, setup direction, stale evidence,
entry gating, text-safe UI rendering and timeframe switching. These are correctness
tests, **not an options backtest or evidence of positive expected returns**.

Still separate: exchange calendar verification, option Greeks/IV history, news,
volume confirmation (index volume is unavailable), trained pattern classifiers,
daily self-learning, a full strategy efficacy study, and a market-hours forward test.
Do not bypass a missing-data blocker to generate activity.

Sources:
- https://groww.in/trade-api/docs/curl/backtesting
- https://developers.openai.com/api/docs/guides/structured-outputs
