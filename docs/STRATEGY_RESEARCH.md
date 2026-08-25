# kiwiT Strategy Research

Run the full deterministic validation suite:

```bash
python scripts/run_strategy_research.py
```

Inputs come from `data/market/published/niftybees_research_adjusted.csv`. Outputs are written under `output/research/strategy_validation_v1/`.

The suite includes:

- next-session passive, EMA, and Donchian simulations;
- fixed historical folds with pre-fold indicator warm-up;
- 0/5/10/20/30 bps cost stress;
- 3x3 predeclared parameter neighborhoods;
- 1,000 seeded randomized-entry controls with unchanged exit logic;
- 10,000 seeded bootstrap trade-sequence simulations;
- explicit all-or-nothing promotion gates.

Random controls test whether the entry rule adds value beyond the exit and the market's general drift. Bootstrap results are descriptive stress estimates, not proof, especially when the observed trade sample is small.

Point-in-time cross-sectional research remains blocked until dated NIFTY 100 membership and all-constituent corporate actions are available.

