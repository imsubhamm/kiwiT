# Deterministic risk policy (KIW-27)

`kiwit.risk_policy.DeterministicRiskPolicy` wraps the existing RiskEngine with mandatory operational gates. Its frozen RiskConfig, HardLimits and versioned TradingCalendar are supplied by trusted application configuration, never model output. The default emergency flag is disabled-for-entry (`emergency_disabled=True`); trusted operators must explicitly enable paper evaluation.

The original engine retains risk-per-trade budgeting, lot rounding, daily/weekly loss limits, drawdown throttle/halt, maximum open positions, aggregate open risk and correlated risk. HardLimits additionally caps final quantity (rounded down to lots), daily trade count, consecutive-loss cooldown, session entry cutoff and state age. Defaults: quantity 1000, 10 trades/day, three losses followed by 1800 seconds cooldown, no new entries during the final 900 session seconds and maximum 30-second state/proposal age. Unknown sessions block. Equality at the cutoff blocks; equality at cooldown expiry permits review. Configuration is recorded with every decision.

RiskState comes from the authoritative paper ledger, with trading date, counters, last loss time and as-of time. Stale/future/mismatched state, invalid counters, nonfinite financial inputs and invalid costs reject. Every failed mandatory rule returns reject with zero quantity. A successful result is paper-only and does not place an order. ML/LLM outputs cannot pass replacement risk limits to evaluate.

Every evaluation writes its decision, reason codes, configuration, calendar version, proposal, portfolio, costs and state to the audit sink before returning. Audit failure propagates. The existing audit sink must be owned by trusted application infrastructure; no model-supplied sink is used.

This module is a local integration point. The deployed legacy execution path has not yet been routed through it. The paper executor must serialize ledger reads, recheck this policy immediately before execution, and atomically update counters/positions so concurrent candidates cannot reuse the same available capacity. Emergency changes require a new trusted configuration instance; a future service control must propagate it before the next evaluation.

Tests cover risk budgeting/quantity cap, daily loss, drawdown, positions, daily trades, cooldown boundary, session cutoff/calendar, emergency disable, stale/nonfinite states and logging failure. This story does not enable live broker orders.
