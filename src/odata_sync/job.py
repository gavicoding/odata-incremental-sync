"""Run one incremental sync from environment variables (the container entry point).

Required:
  ODATA_URL          service root, e.g. https://erp.exemplo.test/odata/
  ODATA_ENTITY       entity set, e.g. Invoices
  ODATA_KEY          key field, e.g. Key
  ODATA_CURSOR       change timestamp field, e.g. ModifiedAt
  TARGET             sqlite:///path/file.db | mysql://user:pass@host:3306/db |
                     postgresql://user:pass@host:5432/db
Optional:
  ODATA_TOKEN        bearer token (better: mount it from Secret Manager)
  ODATA_PAGE_SIZE    default 500
  ODATA_MAX_TOP      largest $top the server accepts
  ODATA_LOOKBACK_MINUTES  default 5
  ODATA_START        ISO date/time with offset for the very first run
  ODATA_DATE_STYLE   offset | utc | local | v2 (default offset)
  ODATA_SERVER_TZ    IANA zone of the server, e.g. America/Sao_Paulo
  TARGET_TABLE       default: the entity name in lowercase
  CHECKPOINT         "target" (default, table odata_checkpoints in TARGET) or a JSON file path,
                     e.g. /mnt/state/checkpoint.json on a mounted Cloud Storage bucket
"""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from .checkpoint import CheckpointStore, JsonFileCheckpoint, SqlCheckpoint
from .client import ODataClient
from .dates import OFFSET, parse_datetime
from .incremental import sync_incremental
from .sinks import MySQLSink, PostgresSink, SqliteSink, SqlSink


@dataclass(frozen=True)
class Target:
    kind: str  # sqlite | mysql | postgres
    path: str = ""
    host: str = ""
    port: int | None = None
    user: str = ""
    password: str = ""
    database: str = ""


def parse_target(url: str) -> Target:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    if scheme == "sqlite":
        # sqlite:///relative.db or sqlite:////absolute/path.db
        path = url.split("://", 1)[1]
        path = path[1:] if path.startswith("/") else path
        if not path:
            raise ValueError("sqlite target needs a file path: sqlite:///data.db")
        return Target("sqlite", path=path)
    kinds = {"mysql": "mysql", "postgres": "postgres", "postgresql": "postgres"}
    if scheme not in kinds:
        raise ValueError(f"unsupported TARGET scheme {scheme!r}; use sqlite, mysql or postgresql")
    database = parsed.path.lstrip("/")
    if not parsed.hostname or not database:
        raise ValueError("TARGET needs host and database: mysql://user:pass@host:3306/db")
    return Target(
        kinds[scheme],
        host=parsed.hostname,
        port=parsed.port,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        database=database,
    )


def open_sink(target: Target, table: str, key: str) -> SqlSink:
    if target.kind == "sqlite":
        return SqliteSink(target.path, table, key, create_table=True)
    if target.kind == "mysql":
        return MySQLSink.connect(
            table,
            key,
            sink_options={"create_table": True},
            host=target.host,
            port=target.port or 3306,
            user=target.user,
            password=target.password,
            database=target.database,
        )
    conninfo = (
        f"host={target.host} port={target.port or 5432} dbname={target.database} "
        f"user={target.user} password={target.password}"
    )
    return PostgresSink.connect(conninfo, table, key, create_table=True)


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ValueError(f"environment variable {name} is required")
    return value


def run(env: Mapping[str, str] | None = None) -> dict[str, Any]:
    env = os.environ if env is None else env
    entity = _require(env, "ODATA_ENTITY")
    key = _require(env, "ODATA_KEY")
    cursor = _require(env, "ODATA_CURSOR")
    target = parse_target(_require(env, "TARGET"))
    server_tz = ZoneInfo(env["ODATA_SERVER_TZ"]) if env.get("ODATA_SERVER_TZ") else None
    client = ODataClient(
        _require(env, "ODATA_URL"),
        token=env.get("ODATA_TOKEN") or None,
        max_top=int(env["ODATA_MAX_TOP"]) if env.get("ODATA_MAX_TOP") else None,
    )
    sink = open_sink(target, env.get("TARGET_TABLE") or entity.lower(), key)
    checkpoint = env.get("CHECKPOINT", "target")
    store: CheckpointStore = (
        SqlCheckpoint(sink.conn, sink.dialect) if checkpoint == "target" else JsonFileCheckpoint(Path(checkpoint))
    )
    start = parse_datetime(env["ODATA_START"], server_tz) if env.get("ODATA_START") else None
    result = sync_incremental(
        client,
        entity,
        key,
        cursor,
        store,
        sink,
        lookback=timedelta(minutes=float(env.get("ODATA_LOOKBACK_MINUTES", "5"))),
        start=start,
        page_size=int(env.get("ODATA_PAGE_SIZE", "500")),
        date_style=env.get("ODATA_DATE_STYLE", OFFSET),
        server_tz=server_tz,
    )
    return {**result.to_dict(), "rows_in_target": sink.count()}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        summary = run()
    except Exception as exc:
        logging.getLogger("odata_sync").error("sync failed: %s", exc)
        return 1
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
