# Changelog

## 1.0.0 (2026-11-26)

First release.

- `ODataClient`: `$top`/`$skip` and `@odata.nextLink` paging, OData v2 and v4 responses,
  `max_top`, retries on 429/5xx/network errors honoring `Retry-After`, clear 401/403 errors.
- `sync_incremental`: checkpoint that only moves after the sink confirms, lookback window,
  keyset paging that survives edits during the run.
- `reconcile_by_keys`: per-day key listing and download of missing records in chunks limited by
  count and filter length, `in` or `or` filters.
- Checkpoints: JSON file (atomic), SQLite, any SQL connection, memory.
- Sinks with idempotent upsert: SQLite, MySQL, PostgreSQL.
- Explicit time zones everywhere, four literal styles.
- Container entry point `odata-sync-job`, Dockerfile and a Cloud Run guide.
- Fake OData server for demos and tests.
