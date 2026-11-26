"""Incremental sync, checkpoints, sinks and reconciliation against the fake server."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from fake_server import LOCAL_TZ, FakeODataService, generate_invoices, parse_filter, serve
from odata_sync import (
    JsonFileCheckpoint,
    MemoryCheckpoint,
    ODataClient,
    ODataError,
    SqliteCheckpoint,
    SqliteSink,
    reconcile_by_keys,
    sync_incremental,
)
from odata_sync.sql import MYSQL, POSTGRES, SQLITE, get_dialect


@pytest.fixture
def server():
    service = FakeODataService({"Invoices": generate_invoices(240, days=6)}, page_cap=50)
    srv, url = serve(service)
    yield service, url
    srv.shutdown()


def make_sink(tmp_path, name="db.sqlite"):
    return SqliteSink(tmp_path / name, "invoices", "Key", create_table=True)


def run(url, store, sink, **kw):
    kw.setdefault("server_tz", LOCAL_TZ)
    kw.setdefault("page_size", 80)
    return sync_incremental(ODataClient(url, backoff=0), "Invoices", "Key", "ModifiedAt", store, sink, **kw)


def test_first_run_loads_everything_with_keyset_paging(server, tmp_path):
    service, url = server
    sink = make_sink(tmp_path)
    store = MemoryCheckpoint()
    result = run(url, store, sink)
    assert result.records == 240
    assert sink.count() == 240
    assert result.page_sizes == [50, 50, 50, 50, 40]  # server caps at 50 even though we asked 80
    newest = max(datetime.fromisoformat(r["ModifiedAt"]) for r in service.entities["Invoices"])
    assert datetime.fromisoformat(store.get("Invoices")) == newest
    # keyset paging: no $skip anywhere
    assert not any("$skip" in line for line in service.log)


def test_second_run_reads_only_changes_plus_lookback(server, tmp_path):
    service, url = server
    sink, store = make_sink(tmp_path), MemoryCheckpoint()
    run(url, store, sink)
    service.touch("Invoices", ["NF000003", "NF000100"], datetime(2026, 11, 20, 8, 0, tzinfo=LOCAL_TZ))
    result = run(url, store, sink, lookback=timedelta(0))
    assert result.records == 3  # 2 changed + the boundary row of the old checkpoint ("ge")
    assert result.checkpoint_after == "2026-11-20T08:00:00-03:00"
    again = run(url, store, sink, lookback=timedelta(0))
    assert again.records == 2  # "ge" re-reads the boundary; upsert keeps it harmless
    assert sink.count() == 240


def test_rerun_is_idempotent(server, tmp_path):
    _, url = server
    sink = make_sink(tmp_path)
    run(url, MemoryCheckpoint(), sink)
    run(url, MemoryCheckpoint(), sink)  # fresh checkpoint: everything again
    assert sink.count() == 240


def test_checkpoint_does_not_move_when_the_sink_fails(server, tmp_path):
    _, url = server
    store = MemoryCheckpoint()
    real = make_sink(tmp_path)
    calls = []

    def flaky(records):
        calls.append(len(records))
        if len(calls) == 3:
            raise RuntimeError("database is down")
        return real(records)

    with pytest.raises(RuntimeError):
        run(url, store, flaky)
    saved = store.get("Invoices")
    assert real.count() == 100  # two pages confirmed
    result = run(url, store, real)  # resumes from the checkpoint of page 2
    assert result.checkpoint_before == saved
    assert real.count() == 240


def test_start_and_extra_filter_and_select(server, tmp_path):
    service, url = server
    sink = make_sink(tmp_path)
    start = datetime(2026, 11, 4, tzinfo=LOCAL_TZ)
    result = run(url, MemoryCheckpoint(), sink, start=start, extra_filter="Total gt 1000", select=["Total"])
    expected = [
        r
        for r in service.entities["Invoices"]
        if datetime.fromisoformat(r["ModifiedAt"]) >= start and r["Total"] > 1000
    ]
    assert result.records == len(expected) > 0
    stored = json.loads(sink.conn.execute("SELECT payload FROM invoices LIMIT 1").fetchone()[0])
    assert set(stored) == {"Key", "ModifiedAt", "Total"}
    with pytest.raises(ValueError, match="explicit time zone"):
        run(url, MemoryCheckpoint(), sink, start=datetime(2026, 11, 4))


def test_edits_during_the_run_are_not_skipped(tmp_path):
    """The $skip trap: a row edited mid-run moves to the end and shifts the others."""
    service = FakeODataService({"Invoices": generate_invoices(60, days=2)})
    srv, url = serve(service)
    try:
        sink, seen = make_sink(tmp_path), []

        def sink_and_edit(records):
            seen.extend(r["Key"] for r in records)
            if len(seen) == 20:  # after page 1, edit a row that was already read
                service.touch("Invoices", [records[0]["Key"]], datetime(2026, 12, 1, tzinfo=LOCAL_TZ))
            return sink(records)

        run(url, MemoryCheckpoint(), sink_and_edit, page_size=20)
        assert sink.count() == 60
        assert len(set(seen)) == 60
    finally:
        srv.shutdown()


def test_no_progress_is_detected():
    class StuckClient:
        requests_made = 0

        def fetch_page(self, *args, **kwargs):
            return [{"Key": "A", "ModifiedAt": "2026-11-01T10:00:00.1234567-03:00"}], None

    with pytest.raises(ODataError, match="no progress"):
        sync_incremental(StuckClient(), "X", "Key", "ModifiedAt", MemoryCheckpoint(), lambda r: None)


def test_retries_inside_a_sync(server, tmp_path):
    service, url = server
    service.fail_next(429, times=2, retry_after="0")
    service.fail_next(503, times=1, retry_after=None)
    sink = make_sink(tmp_path)
    result = run(url, MemoryCheckpoint(), sink)
    assert sink.count() == 240
    assert result.requests == 6 + 3  # 5 data pages + final empty page + 3 retries


def test_auth_error_from_server(tmp_path):
    service = FakeODataService(token="token-ficticio")
    srv, url = serve(service)
    try:
        from odata_sync import AuthError

        with pytest.raises(AuthError):
            run(url, MemoryCheckpoint(), make_sink(tmp_path))
        ok = sync_incremental(
            ODataClient(url, token="token-ficticio"),
            "Invoices",
            "Key",
            "ModifiedAt",
            MemoryCheckpoint(),
            make_sink(tmp_path, "b.sqlite"),
            server_tz=LOCAL_TZ,
        )
        assert ok.records == 300
    finally:
        srv.shutdown()


# -- checkpoints ---------------------------------------------------------------------------


def test_json_checkpoint(tmp_path):
    path = tmp_path / "state" / "cp.json"
    store = JsonFileCheckpoint(path)
    assert store.get("A") is None
    store.set("A", "2026-11-01T00:00:00+00:00")
    store.set("B", "x")
    assert JsonFileCheckpoint(path).get("A") == "2026-11-01T00:00:00+00:00"
    assert json.loads(path.read_text()) == {"A": "2026-11-01T00:00:00+00:00", "B": "x"}
    assert [p.name for p in path.parent.iterdir()] == ["cp.json"]  # no temp files left
    path.write_text("", encoding="utf-8")
    assert store.get("A") is None
    path.write_text("[1]", encoding="utf-8")
    with pytest.raises(ValueError):
        store.get("A")


def test_sqlite_checkpoint(tmp_path):
    store = SqliteCheckpoint(tmp_path / "cp.db")
    assert store.get("Invoices") is None
    store.set("Invoices", "v1")
    store.set("Invoices", "v2")
    assert store.get("Invoices") == "v2"
    store.close()
    assert SqliteCheckpoint(tmp_path / "cp.db").get("Invoices") == "v2"


def test_checkpoint_resumes_across_stores(server, tmp_path):
    _, url = server
    sink = make_sink(tmp_path)
    store = SqliteCheckpoint(tmp_path / "cp.db")
    run(url, store, sink)
    second = run(url, SqliteCheckpoint(tmp_path / "cp.db"), sink, lookback=timedelta(0))
    assert second.records == 1  # only the boundary row, re-read by "ge"


# -- sinks and SQL -------------------------------------------------------------------------


def test_sink_with_columns_upserts(tmp_path):
    sink = SqliteSink(tmp_path / "c.db", "t", "Key", columns=["Total", "Meta"], create_table=True)
    assert sink([{"Key": "A", "Total": 1, "Meta": {"b": 1}}, {"Key": "B", "Total": 2}]) == 2
    sink([{"Key": "A", "Total": 9, "Meta": [1, True]}])
    rows = dict(sink.conn.execute("SELECT Key, Total FROM t").fetchall())
    assert rows == {"A": "9", "B": "2"}
    assert sink.conn.execute("SELECT Meta FROM t WHERE Key='A'").fetchone()[0] == "[1, true]"
    assert sink.existing_keys(["A", "Z", "B"]) == {"A", "B"}
    assert sink([]) == 0


def test_sink_rejects_records_without_key_and_rolls_back(tmp_path):
    conn = sqlite3.connect(tmp_path / "r.db")
    sink = SqliteSink(conn, "t", "Key", create_table=True)
    with pytest.raises(ValueError, match="without 'Key'"):
        sink([{"Key": "A"}, {"Other": 1}])
    assert sink.count() == 0
    conn.execute("DROP TABLE t")
    with pytest.raises(sqlite3.OperationalError):
        sink([{"Key": "A"}])


def test_upsert_sql_per_dialect():
    assert SQLITE.upsert("t", ["Key", "V"], "Key") == (
        'INSERT INTO "t" ("Key", "V") VALUES (?, ?) ON CONFLICT ("Key") DO UPDATE SET "V" = excluded."V"'
    )
    assert MYSQL.upsert("t", ["Key", "V"], "Key") == (
        "INSERT INTO `t` (`Key`, `V`) VALUES (%s, %s) ON DUPLICATE KEY UPDATE `V` = VALUES(`V`)"
    )
    assert "ON CONFLICT" in POSTGRES.upsert("t", ["Key", "V"], "Key")
    assert MYSQL.upsert("t", ["Key"], "Key").startswith("INSERT IGNORE")
    assert POSTGRES.upsert("t", ["Key"], "Key").endswith("DO NOTHING")
    assert "VARCHAR(255) NOT NULL PRIMARY KEY" in MYSQL.create_table("t", ["Key", "V"], "Key")


@pytest.mark.parametrize("name", ["t; DROP TABLE x", "1abc", "a-b", ""])
def test_identifiers_are_validated(name):
    with pytest.raises(ValueError, match="invalid SQL identifier"):
        SQLITE.ident(name)


def test_get_dialect():
    assert get_dialect("mysql") is MYSQL
    assert get_dialect(POSTGRES) is POSTGRES
    with pytest.raises(ValueError):
        get_dialect("oracle")


# -- reconciliation ------------------------------------------------------------------------


def test_reconcile_finds_and_fetches_only_missing(server, tmp_path):
    service, url = server
    sink = make_sink(tmp_path)
    run(url, MemoryCheckpoint(), sink)
    lost = ["NF000005", "NF000050", "NF000051", "NF000200"]
    sink.conn.execute(f"DELETE FROM invoices WHERE Key IN ({','.join('?' * 4)})", lost)
    sink.conn.commit()
    service.log.clear()
    report = reconcile_by_keys(
        ODataClient(url),
        "Invoices",
        "Key",
        "IssuedAt",
        date(2026, 11, 1),
        date(2026, 11, 6),
        existing_keys=sink.existing_keys,
        sink=sink,
        tz=LOCAL_TZ,
        in_chunk=2,
    )
    assert report.remote == 240
    assert report.missing == 4
    assert report.fetched == 4
    assert report.still_missing == []
    assert all(d.ok for d in report.days)
    assert sink.count() == 240
    key_requests = [line for line in service.log if "$select=Key" in line]
    assert len(key_requests) >= 6  # at least one key listing per day
    assert "total" in report.table()
    assert report.to_dict()["days"][0]["day"] == "2026-11-01"


def test_reconcile_without_fetch_and_with_or_style(server):
    service, url = server
    have = {r["Key"] for r in service.entities["Invoices"]} - {"NF000010"}
    report = reconcile_by_keys(
        ODataClient(url),
        "Invoices",
        "Key",
        "IssuedAt",
        date(2026, 11, 1),
        date(2026, 11, 6),
        existing_keys=lambda keys: {k for k in keys if k in have},
        fetch_missing=False,
        tz=LOCAL_TZ,
    )
    assert report.missing == 1 and report.fetched == 0
    report = reconcile_by_keys(
        ODataClient(url),
        "Invoices",
        "Key",
        "IssuedAt",
        date(2026, 11, 1),
        date(2026, 11, 6),
        existing_keys=lambda keys: {k for k in keys if k in have},
        tz=LOCAL_TZ,
        in_style="or",
    )
    assert report.fetched == 1


def test_reconcile_respects_filter_size_limit(server, tmp_path):
    service, url = server
    sink = make_sink(tmp_path)
    service.log.clear()
    report = reconcile_by_keys(
        ODataClient(url),
        "Invoices",
        "Key",
        "IssuedAt",
        date(2026, 11, 1),
        date(2026, 11, 1),
        existing_keys=lambda keys: set(),
        sink=sink,
        tz=LOCAL_TZ,
        in_chunk=40,
        max_filter_chars=120,
    )
    fetches = [line for line in service.log if "%20in%20" in line]
    assert report.fetched == report.remote == sink.count()
    assert len(fetches) > report.remote / 40  # more requests than count alone would need


def test_reconcile_reports_keys_that_vanish(server):
    _, url = server

    def sneaky_sink(records):
        return None

    class Vanishing(ODataClient):
        def iter_pages(self, entity, filter=None, **kw):
            if filter and " in " in filter:
                return iter(())
            return super().iter_pages(entity, filter=filter, **kw)

    report = reconcile_by_keys(
        Vanishing(url),
        "Invoices",
        "Key",
        "IssuedAt",
        date(2026, 11, 1),
        date(2026, 11, 1),
        existing_keys=lambda keys: set(keys[1:]),
        sink=sneaky_sink,
        tz=LOCAL_TZ,
    )
    assert len(report.still_missing) == 1
    assert not report.days[0].ok


def test_reconcile_date_range_validation(server):
    _, url = server
    with pytest.raises(ValueError):
        reconcile_by_keys(ODataClient(url), "Invoices", "Key", "IssuedAt", date(2026, 11, 2), date(2026, 11, 1), set)


# -- fake server sanity --------------------------------------------------------------------


def test_fake_server_filter_language():
    r = {"A": 5, "S": "x'y", "T": "2026-11-01T10:00:00-03:00"}
    assert parse_filter("A gt 1 and (S eq 'x''y' or A eq 0)")(r)
    assert parse_filter("not (A lt 5)")(r)
    assert parse_filter("T eq 2026-11-01T13:00:00Z")(r)
    assert parse_filter("T ge datetime'2026-11-01T10:00:00'")(r)
    assert parse_filter("A in (1,5)")(r)
    assert not parse_filter("A ne 5")(r)


def test_fake_server_errors(server):
    service, url = server
    import requests

    assert requests.get(url + "Nope", timeout=5).status_code == 404
    assert requests.get(url + "Invoices?$filter=A%20%3D%3D%201", timeout=5).status_code == 400
    service.max_top = 10
    assert requests.get(url + "Invoices?$top=11", timeout=5).status_code == 400


def test_dates_compare_across_offsets(server, tmp_path):
    """A literal in UTC must select the same rows as the same instant in local time."""
    _, url = server
    client = ODataClient(url)
    instant = datetime(2026, 11, 3, 12, 0, tzinfo=LOCAL_TZ)
    utc_rows = list(
        client.iter_records("Invoices", filter=f"ModifiedAt ge {instant.astimezone(timezone.utc):%Y-%m-%dT%H:%M:%SZ}")
    )
    local_rows = list(client.iter_records("Invoices", filter="ModifiedAt ge 2026-11-03T12:00:00-03:00"))
    assert len(utc_rows) == len(local_rows) > 0
