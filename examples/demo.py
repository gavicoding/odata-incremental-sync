"""End-to-end demo against the local fake server. Everything here is fictional.

    python examples/demo.py

1. full first load into SQLite (server caps pages at 100 and fails twice with 429);
2. a few invoices change on the server, the next run reads only those;
3. rows go missing locally, the key-only reconciliation finds and downloads just those.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_server import LOCAL_TZ, FakeODataService, generate_invoices, serve
from odata_sync import JsonFileCheckpoint, ODataClient, SqliteSink, reconcile_by_keys, sync_incremental


def main() -> None:
    service = FakeODataService({"Invoices": generate_invoices(500)}, page_cap=100)
    server, url = serve(service)
    work = Path(tempfile.mkdtemp(prefix="odata-demo-"))
    db = sqlite3.connect(work / "invoices.db")
    sink = SqliteSink(db, "invoices", "Key", columns=["IssuedAt", "ModifiedAt", "Customer", "Total"], create_table=True)
    store = JsonFileCheckpoint(work / "checkpoint.json")
    client = ODataClient(url, backoff=0.1)

    print("1) First run: full load")
    service.fail_next(429, times=2, retry_after="0")
    r = sync_incremental(client, "Invoices", "Key", "ModifiedAt", store, sink, page_size=250, server_tz=LOCAL_TZ)
    print(f"   {r.records} records in {r.pages} pages, {r.requests} requests (2 were 429 retries)")
    print(f"   checkpoint: {r.checkpoint_after}   rows in SQLite: {sink.count()}")

    print("2) Second run: only what changed")
    changed = ["NF000007", "NF000123", "NF000456"]
    service.touch("Invoices", changed, datetime(2026, 11, 12, 9, 30, tzinfo=LOCAL_TZ))
    r = sync_incremental(client, "Invoices", "Key", "ModifiedAt", store, sink, page_size=250, server_tz=LOCAL_TZ)
    print(f"   {r.records} records read (lookback 5 min re-reads boundary rows), rows in SQLite: {sink.count()}")

    print("3) Reconciliation: rows lost locally")
    lost = ["NF000010", "NF000200", "NF000201", "NF000333"]
    db.execute(f"DELETE FROM invoices WHERE Key IN ({','.join('?' * len(lost))})", lost)
    db.commit()
    print(f"   deleted {len(lost)} rows, rows in SQLite: {sink.count()}")
    report = reconcile_by_keys(
        client,
        "Invoices",
        "Key",
        "IssuedAt",
        date(2026, 11, 1),
        date(2026, 11, 10),
        existing_keys=sink.existing_keys,
        sink=sink,
        tz=LOCAL_TZ,
    )
    print("   " + report.table().replace("\n", "\n   "))
    print(f"   rows in SQLite: {sink.count()}, still missing: {report.still_missing or 'none'}")
    server.shutdown()
    db.close()


if __name__ == "__main__":
    main()
