# Historical market store and replay (KIW-11)

The V2 historical layer uses the same `market-v1` candles and `IngestResult`
contract as the Groww data service. It is a local SQLite implementation for data
collection, dataset preparation and deterministic replay. It does not place orders
or replace the legacy backtester.

## Stored data

`HistoricalStore` stores immutable raw market batches and canonical candles in a
single transaction, with provenance links between them. Indexes cover instrument,
exchange session date, timeframe and observation time. Timeframes are exact candle
durations, represented internally as integer microseconds. Prices retain decimal
precision; unknown volume remains null. Raw input must contain market data only,
without authorization headers or credentials.

Identical reimports are idempotent. Conflicting candles reject the entire batch,
including its new raw/provenance records. Corrections cannot overwrite an old
observation or move its availability earlier. Use a separate dataset/database for
corrected source versions; automatic revision selection is not implemented.

## Point-in-time contract

Every ingest/import requires a timezone-aware `available_at`: actual receipt time
or a known publication time. Candle close alone does not establish availability.
The store rejects availability before candle close. Repeat ingests retain the
original availability; attempts to backdate it fail.

A query exposes a candle only when **both its close and availability are at or
before the observation clock**. Imported archive exports do not include historical
publication timestamps, so the importer requires that metadata explicitly. If only
today's download time is known, use that time: the import will not appear in an
earlier simulation. Existing observation-store records are not silently migrated,
because they lack trustworthy candle receipt timestamps.

`HistoricalReplay` receives an injected clock. Even if a consumer requests an end
time in the future, it gets only observations available at that clock. The
`banknifty_candles(start, end)` method returns the same `IngestResult` type as
`GrowwMarketDataService`; consumers call `require_valid()` before evaluation.

The lower-level `HistoricalStore.candles` query requires an explicit `as_of`. It
returns canonical data but does not calculate gap reports. Dataset/backtest
consumers should use the replay interface for quality-gated input.

## Sessions, gaps and replay order

`replay_sessions` emits chronologically ordered frames at candle-close ticks in
explicitly declared exchange sessions. Each frame contains the current session's
available prefix, its observation clock, calendar version and quality report.
Session-close ticks are included. Missing entire sessions emit invalid frames;
missing leading, interior and trailing bars are reported by their expected open
timestamps. Future expected bars are not reported as gaps prematurely.

The calendar declares trading sessions; it does not infer weekdays. Holidays have
no declared session; special weekend/short sessions can be listed explicitly.
A range with no declared sessions is rejected. The replay timeframe must divide
each declared session exactly; partial final buckets are not invented. Misaligned
stored candles are flagged as unexpected. A caller-supplied calendar must be
complete for the intended dataset; an omitted session cannot be distinguished
from an intentional closure by this layer.

Late observations appear at the next eligible tick after availability. Data that
arrives after the final session tick is absent from that session's intraday replay.
A later clock-bound snapshot of the historical range can retrieve it; the system
does not retroactively change earlier frames or synthesize an after-hours tick.

## Live ingestion integration

Pass `history=HistoricalStore(path)` to `GrowwMarketDataService` or its
`from_settings` factory. Completed candle observations are then recorded with
receipt time sampled after the Groww request. Only the candle market fields from
the provider payload are retained as raw data. Historical persistence failure
invalidates the ingestion result. The ordinary observation store remains supported;
the two stores do not share a transaction, so a failed operation can leave a
historical batch persisted, and retries are idempotent.

This is an opt-in constructor integration. No deployed service has been activated
or reconfigured by this change, and no network backfill has been run.

## Command-line import and replay

Run with the project virtual environment. Global `--db` comes before the subcommand.

```sh
.venv/bin/python scripts/replay_market_data.py --db data/local/history.sqlite3 import \
  --file data/market/normalized/nse/niftybees.market-v1.jsonl \
  --available-at '2026-09-10T18:00:00+05:30' --source nse-archive
```

The availability above is an example only: supply the actual known receipt or
publication time. One import batch assigns one availability timestamp to its
candles. For per-observation availability, call `record` with separate batches.
The imported bytes are retained as the source batch; when importing normalized
JSONL these are normalized export bytes, not the original exchange ZIP.

Example calendar JSON (illustrative, not an authoritative exchange calendar):

```json
{
  "version": "example-session-v1",
  "sessions": [{"day": "2026-09-10", "opens": "09:15", "closes": "15:30"}]
}
```

```sh
.venv/bin/python scripts/replay_market_data.py --db data/local/history.sqlite3 replay \
  --calendar data/local/calendar.json \
  --start '2026-09-10T09:15:00+05:30' --end '2026-09-10T15:30:00+05:30' \
  --symbol BANKNIFTY --segment INDEX --series INDEX --interval-minutes 1
```

Output is JSONL with canonical candles, observation time, calendar version,
invalid-state reasons, missing opens and unexpected opens. The Bank Nifty example
requires Bank Nifty minute data in the database; the NIFTYBEES archive import above
is a separate example. A regular full-session daily archive candle uses a
375-minute duration and becomes visible only after its supplied availability time.

## Validation

The tests exercise late arrivals, future-value changes, clock cutoff enforcement,
chronological session replay, head/interior/tail/whole-session gaps, special
sessions, index isolation, conflict rollback, raw retention, idempotency,
availability validation, live-to-history receipt timing and CLI import/replay.
