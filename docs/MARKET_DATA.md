# kiwiT Market-Data Pipeline

## Sources

- NSE legacy capital-market bhavcopy through 2024-07-05.
- NSE UDiFF CM final bhavcopy from 2024-07-08.
- NSE daily index snapshot for NIFTY 50.
- Versioned corporate-action reference data.

The UDiFF adapter reads named ISO-tag fields and rejects files missing required columns. The official transition is documented by NSE Circular 62424 and the NSE Forms & Formats page.

## Zones

- `data/market/raw`: immutable downloaded archives, excluded from version control.
- `data/market/manifests`: URL, date, retrieval time, hash, byte size, and status.
- `data/market/normalized`: source-specific normalized bars with per-file hashes.
- `data/market/quarantine`: validation reports for failed ranges.
- `data/market/published`: research-ready unified datasets and their manifest.

## Publication rules

A range is not published when it has duplicate dates, non-positive prices, invalid OHLC relationships, negative volume, unexplained moves above 40%, or an equity trading date with no matching index date. Index-only dates are warnings because an ETF can occasionally lack a valid row.

Corporate actions are never applied to the execution view. The research view is explicitly adjusted and stored separately. Raw downloads are never rewritten.

## Commands

Download and validate the UDiFF period:

```bash
python scripts/ingest_market_data.py --start 2024-07-08 --end 2026-08-20 --workers 16
```

Build the unified execution, research, and index datasets:

```bash
python scripts/build_unified_market_dataset.py
```

## Known limitations

- The free official data sources do not provide one convenient, complete point-in-time NIFTY 100 membership history. The membership model and overlap validation exist, but the dataset is not yet populated.
- Corporate-action ingestion is currently a versioned local reference for NIFTYBEES, not an automated all-equity feed.
- A formal NSE holiday calendar adapter is still needed. Successful matched exchange dates currently define the observed calendar.
- Exact broker-specific transaction costs are outside this pipeline and belong in the execution-cost service.

