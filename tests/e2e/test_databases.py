"""End-to-end sync into real MySQL and PostgreSQL servers.

Skipped unless the connection variables are set (the CI job starts both as service containers):
  MYSQL_HOST, MYSQL_PORT, MYSQL_USER, MYSQL_PASSWORD, MYSQL_DATABASE
  POSTGRES_DSN   e.g. "host=127.0.0.1 port=5432 dbname=sync user=sync password=..."
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta

import pytest

from fake_server import LOCAL_TZ, FakeODataService, generate_invoices, serve
from odata_sync import MySQLSink, ODataClient, PostgresSink, SqlCheckpoint, reconcile_by_keys, sync_incremental

TOTAL = 350


def mysql_sink():
    if not os.environ.get("MYSQL_HOST"):
        pytest.skip("MYSQL_HOST not set")
    pytest.importorskip("pymysql")
    sink = MySQLSink.connect(
        "invoices_e2e",
        "Key",
        sink_options={"columns": ["IssuedAt", "ModifiedAt", "Customer", "Total"], "create_table": True},
        host=os.environ["MYSQL_HOST"],
        port=int(os.environ.get("MYSQL_PORT", "3306")),
        user=os.environ["MYSQL_USER"],
        password=os.environ["MYSQL_PASSWORD"],
        database=os.environ["MYSQL_DATABASE"],
    )
    return sink


def postgres_sink():
    if not os.environ.get("POSTGRES_DSN"):
        pytest.skip("POSTGRES_DSN not set")
    pytest.importorskip("psycopg")
    return PostgresSink.connect(
        os.environ["POSTGRES_DSN"],
        "invoices_e2e",
        "Key",
        columns=["IssuedAt", "ModifiedAt", "Customer", "Total"],
        create_table=True,
    )


@pytest.fixture(params=["mysql", "postgres"])
def sink(request):
    s = mysql_sink() if request.param == "mysql" else postgres_sink()
    cur = s.conn.cursor()
    cur.execute(f"DELETE FROM {s.dialect.ident('invoices_e2e')}")
    cur.execute(f"DROP TABLE IF EXISTS {s.dialect.ident('odata_checkpoints')}")
    s.conn.commit()
    yield s
    cur = s.conn.cursor()
    cur.execute(f"DROP TABLE IF EXISTS {s.dialect.ident('invoices_e2e')}")
    cur.execute(f"DROP TABLE IF EXISTS {s.dialect.ident('odata_checkpoints')}")
    s.conn.commit()
    s.conn.close()


def test_sync_end_to_end(sink):
    service = FakeODataService({"Invoices": generate_invoices(TOTAL, days=7)}, page_cap=60)
    server, url = serve(service)
    try:
        client = ODataClient(url, backoff=0)
        store = SqlCheckpoint(sink.conn, sink.dialect)

        def sync(**kw):
            return sync_incremental(
                client, "Invoices", "Key", "ModifiedAt", store, sink, page_size=100, server_tz=LOCAL_TZ, **kw
            )

        # 1. full load, with a retry in the middle
        service.fail_next(429, times=1, retry_after="0")
        first = sync()
        assert first.records == TOTAL
        assert sink.count() == TOTAL

        # 2. checkpoint stored in the database equals the newest ModifiedAt
        newest = max(datetime.fromisoformat(r["ModifiedAt"]) for r in service.entities["Invoices"])
        assert datetime.fromisoformat(store.get("Invoices")) == newest

        # 3. idempotent: a run from scratch does not duplicate anything
        store.set("Invoices", "2000-01-01T00:00:00+00:00")
        again = sync()
        assert again.records == TOTAL
        assert sink.count() == TOTAL

        # 4. changes on the server update the rows, no new rows
        service.touch("Invoices", ["NF000001", "NF000002"], datetime(2026, 12, 1, 10, tzinfo=LOCAL_TZ))
        delta = sync(lookback=timedelta(0))
        assert delta.records == 3  # 2 changes + boundary row of the previous checkpoint
        assert sink.count() == TOTAL
        cur = sink.conn.cursor()
        cur.execute(
            f"SELECT {sink.dialect.ident('ModifiedAt')} FROM {sink.dialect.ident('invoices_e2e')} "
            f"WHERE {sink.dialect.ident('Key')} = {sink.dialect.placeholder}",
            ("NF000001",),
        )
        assert cur.fetchone()[0] == "2026-12-01T10:00:00-03:00"
        sink.conn.commit()

        # 5. reconciliation fetches only the rows deleted locally
        cur.execute(
            f"DELETE FROM {sink.dialect.ident('invoices_e2e')} WHERE {sink.dialect.ident('Key')} "
            f"IN ({sink.dialect.params(3)})",
            ("NF000010", "NF000020", "NF000030"),
        )
        sink.conn.commit()
        report = reconcile_by_keys(
            client,
            "Invoices",
            "Key",
            "IssuedAt",
            date(2026, 11, 1),
            date(2026, 11, 7),
            existing_keys=sink.existing_keys,
            sink=sink,
            tz=LOCAL_TZ,
            in_chunk=2,
        )
        assert report.remote == TOTAL
        assert report.missing == 3
        assert report.fetched == 3
        assert sink.count() == TOTAL
    finally:
        server.shutdown()
