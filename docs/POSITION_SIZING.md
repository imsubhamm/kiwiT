# Position, stop and target calculator (KIW-28)

`kiwit.sizing.calculate_plan` uses trusted `SizingRules` and numeric `SizingInput` to produce an immutable-fingerprint plan. There is no input quantity, stop or target that an LLM can supply. Inputs contain instrument/snapshot binding, direction, reference price, equity, available capital, tick/lot size, ATR where required and conservative per-unit cost assumptions.

Versioned rules select either ATR times a multiplier or a fixed entry-price fraction for stop distance. Long entries round up to the tick; short entries round down. Stops round outward, then the target is derived from the actual rounded stop distance and rounded outward. Nonpositive or invalid directional geometry rejects.

Sizing uses Decimal arithmetic under a fixed precision context. The per-unit loss is rounded stop distance plus round-trip cost (including the caller's adverse slippage/fee allowance). Quantity is the minimum allowed by equity risk budget, available unlevered capital and maximum quantity, rounded down to a whole lot. Entry costs are reserved in the capital calculation. Net reward/risk subtracts round-trip cost from target reward and adds it to stop risk; insufficient reward/risk or less than one affordable lot rejects with zero quantity.

The output includes exact rounded prices, quantity, budget, planned loss, reward/risk, rule/input versions and reason codes. CALCULATED_REQUIRES_RISK is not execution authorization. Risk policy must re-evaluate current ledger state before any paper order and must not increase the calculated quantity. If risk reduces quantity after the critic reviewed a proposal, rebuild and re-review the changed proposal rather than silently altering its binding.

Capital treatment is deliberately unlevered for both directions; this is not a broker-specific margin model. Planned loss respects configured limits under the supplied cost assumptions; gaps or worse actual slippage can exceed the plan. Future broker-specific constraints must add gates rather than weakening this calculation.

The calculator and tests are local. Existing deployed legacy sizing paths have not been rerouted; integration into the V2 paper pipeline remains pending. No live orders are enabled.
