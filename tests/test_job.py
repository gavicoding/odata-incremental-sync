from __future__ import annotations

import json
import sqlite3

import pytest

from fake_server import FakeODataService, generate_invoices, serve
from odata_sync import job


@pytest.fixture
def url():
    srv, base = serve(FakeODataService({"Invoices": generate_invoices(120)}, page_cap=40))
    yield base
    srv.shutdown()


def env(url, db, **extra):
    return {
        "ODATA_URL": url,
        "ODATA_ENTITY": "Invoices",
        "ODATA_KEY": "Key",
        "ODATA_CURSOR": "ModifiedAt",
        "TARGET": f"sqlite:///{db.as_posix()}",
        "ODATA_SERVER_TZ": "America/Sao_Paulo",
        **extra,
    }


def test_job_with_checkpoint_in_the_target(url, tmp_path):
    db = tmp_path / "out.db"
    first = job.run(env(url, db, ODATA_PAGE_SIZE="50"))
    assert first["records"] == 120
    assert first["rows_in_target"] == 120
    second = job.run(env(url, db, ODATA_LOOKBACK_MINUTES="0"))
    assert second["checkpoint_before"] == first["checkpoint_after"]
    assert second["records"] == 1
    conn = sqlite3.connect(db)
    assert conn.execute("SELECT COUNT(*) FROM invoices").fetchone()[0] == 120
    assert conn.execute("SELECT value FROM odata_checkpoints").fetchone()[0] == first["checkpoint_after"]


def test_job_with_json_checkpoint_and_start(url, tmp_path):
    cp = tmp_path / "state" / "cp.json"
    out = job.run(
        env(url, tmp_path / "o.db", CHECKPOINT=str(cp), ODATA_START="2026-11-08T00:00:00-03:00", TARGET_TABLE="nf")
    )
    assert 0 < out["records"] < 120
    assert json.loads(cp.read_text())["Invoices"] == out["checkpoint_after"]


def test_job_requires_variables(tmp_path):
    with pytest.raises(ValueError, match="ODATA_ENTITY"):
        job.run({})


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite:///data/out.db", job.Target("sqlite", path="data/out.db")),
        ("sqlite:////var/lib/out.db", job.Target("sqlite", path="/var/lib/out.db")),
        (
            "mysql://sync:a%40b@db.exemplo.test:3307/erp",
            job.Target("mysql", host="db.exemplo.test", port=3307, user="sync", password="a@b", database="erp"),
        ),
        (
            "postgresql://sync:x@10.0.0.5/erp",
            job.Target("postgres", host="10.0.0.5", user="sync", password="x", database="erp"),
        ),
    ],
)
def test_parse_target(url, expected):
    assert job.parse_target(url) == expected


@pytest.mark.parametrize("bad", ["sqlite://", "oracle://h/db", "mysql://host-only", "postgresql:///db"])
def test_parse_target_errors(bad):
    with pytest.raises(ValueError):
        job.parse_target(bad)


def test_main_prints_summary_and_exit_codes(url, tmp_path, monkeypatch, capsys):
    for k, v in env(url, tmp_path / "m.db").items():
        monkeypatch.setenv(k, v)
    assert job.main() == 0
    assert json.loads(capsys.readouterr().out)["records"] == 120
    monkeypatch.setenv("TARGET", "oracle://x/y")
    assert job.main() == 1
