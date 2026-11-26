# odata-incremental-sync

[![CI](https://github.com/gavicoding/odata-incremental-sync/actions/workflows/ci.yml/badge.svg)](https://github.com/gavicoding/odata-incremental-sync/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%20to%203.13-blue)
![Databases](https://img.shields.io/badge/tested%20on-MySQL%208%20%7C%20PostgreSQL%2016%20%7C%20SQLite-informational)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

English | [Português](README.pt-BR.md)

Incremental sync and key-only reconciliation for OData APIs (ERPs, fiscal platforms, Dynamics,
SAP Gateway): checkpoints that only move after your data is saved, safe paging, retries with
backoff, and a cheap way to find and fill the gaps.

## The problem

Most OData integrations are written the same way: read everything again every night with
`$top`/`$skip`, no retry, no idea of what was missed. That is slow, times out on big entities
and leaves silent holes:

- a 429 or a 503 in the middle and the job dies, or worse, carries on with half the data;
- the API caps `$top` at 100 and the loop quietly stops after the first page;
- a record edited while the job runs shifts the `$skip` window and another record is skipped;
- the filter sends `Z` (UTC) to a server that compares in local time, and three hours of changes
  never arrive;
- nobody knows which invoices are missing until someone complains.

This library fixes each of those with small, tested pieces.

## Install

Requires Python 3.10 or newer.

```bash
pip install "odata-incremental-sync @ git+https://github.com/gavicoding/odata-incremental-sync.git"
# with database drivers:
pip install "odata-incremental-sync[mysql,postgres] @ git+https://github.com/gavicoding/odata-incremental-sync.git"
```

## Usage

### Incremental sync

```python
from datetime import timedelta
from zoneinfo import ZoneInfo
from odata_sync import ODataClient, SqliteSink, SqliteCheckpoint, sync_incremental

client = ODataClient("https://erp.exemplo.test/odata/", token="...", max_top=100)
sink = SqliteSink("erp.db", "invoices", "Key", create_table=True)
store = SqliteCheckpoint("erp.db")

result = sync_incremental(
    client,
    "Invoices",
    key_field="Key",
    cursor_field="ModifiedAt",
    store=store,
    sink=sink,
    lookback=timedelta(minutes=5),  # re-read a small window, upsert makes it harmless
    server_tz=ZoneInfo("America/Sao_Paulo"),
)
print(result.records, result.checkpoint_after)
```

What it does:

1. reads the checkpoint and asks for `ModifiedAt ge (checkpoint - lookback)`, ordered by
   `ModifiedAt, Key`;
2. hands each page to the sink (any callable: a database, a queue, a file);
3. **only after the sink returns** saves the last `ModifiedAt` of that page as the checkpoint.
   If the sink raises, the checkpoint stays where it was and the next run resumes from there.

Pages are walked with **keyset paging**
(`ModifiedAt gt last or (ModifiedAt eq last and Key gt last_key)`) instead of `$skip`, so edits
made on the server while the job runs cannot make it skip a record. There is a test for exactly
that case.

### Key-only reconciliation

```python
from datetime import date
from odata_sync import reconcile_by_keys

report = reconcile_by_keys(
    client,
    "Invoices",
    key_field="Key",
    date_field="IssuedAt",
    start=date(2026, 11, 1),
    end=date(2026, 11, 10),
    existing_keys=sink.existing_keys,  # which of these keys do I already have?
    sink=sink,  # where the missing records go
    in_chunk=40,  # many APIs limit the filter size
    tz=ZoneInfo("America/Sao_Paulo"),  # days are cut in the business calendar
)
print(report.table())
```

For each day it lists **only the keys** (`$select=Key`), asks you which ones you already have,
and downloads the full records of the missing ones with `Key in (...)` in chunks. Listing keys
is cheap; downloading 4 invoices instead of 500 is the whole point. Use `in_style="or"` for
servers without the OData 4.01 `in` operator, and `max_filter_chars` when the limit is the
length of the filter rather than the number of keys.

### Client

```python
client = ODataClient(
    base_url,
    token=None,
    auth=None,  # bearer token, or any requests auth (e.g. ("user", "pass"))
    timeout=30,
    max_retries=5,
    backoff=1.0,
    max_backoff=60,
    max_top=100,  # page sizes above this are reduced
)
for page in client.iter_pages("Customers", filter="Active eq true", orderby="Id", page_size=500):
    ...
```

- `iter_pages` follows `@odata.nextLink` when the server sends it and falls back to
  `$top`/`$skip` otherwise; OData v4 (`value`) and v2 (`d.results`, `__next`) responses work.
- Retries 429, 500, 502, 503, 504, connection errors and timeouts with exponential backoff,
  honoring `Retry-After` (seconds or HTTP date).
- 401 and 403 raise `AuthError` right away with a clear message; other 4xx raise `ODataError`
  without retrying.
- Query values are encoded with `%20`, never `+`, and the `+` of time zone offsets is escaped.

### Checkpoints and sinks

| Checkpoint | Where |
| --- | --- |
| `JsonFileCheckpoint(path)` | JSON file, atomic writes (works on a mounted Cloud Storage bucket) |
| `SqliteCheckpoint(path)` | SQLite table |
| `SqlCheckpoint(conn, "mysql" or "postgres")` | Table in the same database as the data |
| `MemoryCheckpoint()` | Tests and one-off runs |

| Sink | Upsert |
| --- | --- |
| `SqliteSink(path or conn, table, key)` | `INSERT ... ON CONFLICT DO UPDATE` |
| `MySQLSink(conn, ...)` / `MySQLSink.connect(...)` | `INSERT ... ON DUPLICATE KEY UPDATE` |
| `PostgresSink(conn, ...)` / `PostgresSink.connect(conninfo, ...)` | `INSERT ... ON CONFLICT DO UPDATE` |

Without `columns`, a sink stores each record as JSON next to the key, so you can start without
designing a table. With `columns=[...]` each field gets its own column. Table and column names
are validated, so they cannot inject SQL.

## The time zone trap

OData servers disagree on what a datetime literal means. Some store local time and compare the
literal as text; some honor the offset; OData v2 uses `datetime'...'` with no offset at all.
Sending `2026-11-26T10:00:00Z` to a server that keeps `ModifiedAt` in UTC-3 asks for changes
since 07:00 local, or, if the server ignores the `Z`, since 10:00 local: three hours of changes
silently skipped.

This library **never accepts a naive datetime** and makes you choose the literal style:

| `date_style` | Literal sent for 13:00 UTC, `server_tz=America/Sao_Paulo` |
| --- | --- |
| `offset` (default) | `2026-11-26T10:00:00-03:00` |
| `utc` | `2026-11-26T13:00:00Z` |
| `local` | `2026-11-26T10:00:00` |
| `v2` | `datetime'2026-11-26T10:00:00'` |

`server_tz` is also the zone assumed for timestamps the server sends without an offset. Test
once by hand: filter on a record you know, in each style, and see which returns it.

## Example output

The demo runs against a local fake OData server (`examples/fake_server.py`, fictional
invoices) that caps pages at 100 and fails the first two requests with 429:

```text
$ python examples/demo.py
1) First run: full load
   500 records in 5 pages, 8 requests (2 were 429 retries)
   checkpoint: 2026-11-10T22:25:00-03:00   rows in SQLite: 500
2) Second run: only what changed
   4 records read (lookback 5 min re-reads boundary rows), rows in SQLite: 500
3) Reconciliation: rows lost locally
   deleted 4 rows, rows in SQLite: 496
   day         remote  present  missing  fetched
   2026-11-01      37       36        1        1
   2026-11-02      56       55        1        1
   2026-11-03      43       43        0        0
   2026-11-04      44       44        0        0
   2026-11-05      49       48        1        1
   2026-11-06      58       57        1        1
   2026-11-07      53       53        0        0
   2026-11-08      52       52        0        0
   2026-11-09      56       56        0        0
   2026-11-10      52       52        0        0
   total          500      496        4        4
   rows in SQLite: 500, still missing: none
```

You can also run the fake server on its own: `python examples/fake_server.py --port 8080`.

## Container and Google Cloud

The `Dockerfile` builds a small image whose entry point (`odata-sync-job`) runs one sync
configured by environment variables (`ODATA_URL`, `ODATA_ENTITY`, `ODATA_KEY`, `ODATA_CURSOR`,
`TARGET=sqlite:///... | mysql://... | postgresql://...`, see `src/odata_sync/job.py`) and prints
a JSON summary.

[docs/cloud-run.md](docs/cloud-run.md) is a step-by-step guide to run it as a **Cloud Run Job**
scheduled by **Cloud Scheduler**, with the checkpoint in **Cloud SQL** (MySQL or PostgreSQL) or
in a **Cloud Storage** bucket. It is a guide: the CI only builds the image and has no cloud
account.

## Tests

- Unit tests (Python 3.10 to 3.13): paging, nextLink, `$top` limit, retries with `Retry-After`,
  401/403, checkpoints (file and SQLite), idempotency, a sink that fails mid-run, edits during the
  run, reconciliation with gaps, filter size limits, time zones.
- End-to-end job on **MySQL 8** and **PostgreSQL 16** service containers: full sync against the
  fake server, row count, a second run that must not duplicate, checkpoint stored in the
  database, updates, and reconciliation of deleted rows. Passwords are generated per CI run.
- The container entry point runs against PostgreSQL in CI, and the Docker image is built on every
  push.

## Known limitations

- Deletes on the server are not detected by the incremental sync (OData has no standard
  "deleted since"). Reconciliation lists what exists, so you can compare the other way too.
- Keyset paging needs a cursor field and a key the server can sort and compare. Composite keys
  are not supported yet.
- If the server keeps more fractional digits than microseconds and two rows share the same
  timestamp, keyset paging stops with a clear error instead of looping.
- `$expand` and batch requests are out of scope.

## Development

```bash
pip install -e ".[dev]"
ruff check . && ruff format --check . && pytest
python examples/demo.py
```

## License

[MIT](LICENSE). All data in the examples and tests is fictional.
