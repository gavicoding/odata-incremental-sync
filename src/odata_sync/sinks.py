"""Ready-made destinations. Every sink upserts by key, so writing the same page twice is safe.

A sink is any callable that takes a list of records and returns when they are stored. If it
raises, the sync stops and the checkpoint does not move.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from .sql import MYSQL, POSTGRES, SQLITE, Dialect

PAYLOAD = "payload"


def _cell(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    if isinstance(value, bool):
        return int(value)
    return value


class SqlSink:
    """Upsert records into a table.

    ``columns=None`` stores each record as JSON in a ``payload`` column next to the key, which
    needs no table design up front. With ``columns`` each field goes to its own column.
    ``create_table=True`` creates the table if missing (all columns as text); otherwise create
    it yourself with proper types and a primary key or unique index on ``key_field``.
    """

    dialect: Dialect = SQLITE

    def __init__(
        self,
        connection: Any,
        table: str,
        key_field: str,
        columns: list[str] | None = None,
        create_table: bool = False,
    ) -> None:
        self.conn = connection
        self.table = table
        self.key_field = key_field
        self.columns = list(columns) if columns else None
        if self.columns is not None and key_field not in self.columns:
            self.columns.insert(0, key_field)
        self._db_columns = self.columns or [key_field, PAYLOAD]
        self._sql = self.dialect.upsert(table, self._db_columns, key_field)
        if create_table:
            cur = self.conn.cursor()
            cur.execute(self.dialect.create_table(table, self._db_columns, key_field))
            self.conn.commit()

    def _row(self, record: dict) -> tuple:
        if record.get(self.key_field) in (None, ""):
            raise ValueError(f"record without {self.key_field!r}: {str(record)[:200]}")
        key = str(record[self.key_field])
        if self.columns is None:
            return (key, _cell(record))
        return tuple(key if c == self.key_field else _cell(record.get(c)) for c in self.columns)

    def __call__(self, records: list[dict]) -> int:
        rows = [self._row(r) for r in records]
        if not rows:
            return 0
        cur = self.conn.cursor()
        try:
            cur.executemany(self._sql, rows)
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(rows)

    def existing_keys(self, keys: Iterable[str], chunk: int = 500) -> set[str]:
        """Which of ``keys`` are already stored. Plugs straight into ``reconcile_by_keys``."""
        keys = [str(k) for k in keys]
        found: set[str] = set()
        d = self.dialect
        cur = self.conn.cursor()
        for i in range(0, len(keys), chunk):
            part = keys[i : i + chunk]
            cur.execute(
                f"SELECT {d.ident(self.key_field)} FROM {d.ident(self.table)} "
                f"WHERE {d.ident(self.key_field)} IN ({d.params(len(part))})",
                part,
            )
            found.update(str(row[0]) for row in cur.fetchall())
        self.conn.commit()
        return found

    def count(self) -> int:
        cur = self.conn.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {self.dialect.ident(self.table)}")
        (n,) = cur.fetchone()
        self.conn.commit()
        return int(n)


class SqliteSink(SqlSink):
    """``INSERT ... ON CONFLICT DO UPDATE`` into SQLite. Accepts a path or a connection."""

    dialect = SQLITE

    def __init__(self, database: Any, table: str, key_field: str, **kwargs: Any) -> None:
        conn = database if isinstance(database, sqlite3.Connection) else sqlite3.connect(str(database))
        super().__init__(conn, table, key_field, **kwargs)


class MySQLSink(SqlSink):
    """``INSERT ... ON DUPLICATE KEY UPDATE`` into MySQL / MariaDB / Cloud SQL for MySQL.

    Pass a PyMySQL connection, or use ``MySQLSink.connect(...)`` with PyMySQL's arguments.
    Install the driver with ``pip install odata-incremental-sync[mysql]``.
    """

    dialect = MYSQL

    @classmethod
    def connect(cls, table: str, key_field: str, sink_options: dict | None = None, **connect_kwargs: Any):
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ImportError("MySQLSink needs PyMySQL: pip install odata-incremental-sync[mysql]") from exc
        connect_kwargs.setdefault("charset", "utf8mb4")
        return cls(pymysql.connect(**connect_kwargs), table, key_field, **(sink_options or {}))


class PostgresSink(SqlSink):
    """``INSERT ... ON CONFLICT DO UPDATE`` into PostgreSQL / Cloud SQL for PostgreSQL.

    Pass a psycopg (v3) connection, or use ``PostgresSink.connect(conninfo, ...)``.
    Install the driver with ``pip install odata-incremental-sync[postgres]``.
    """

    dialect = POSTGRES

    @classmethod
    def connect(cls, conninfo: str, table: str, key_field: str, **sink_options: Any):
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise ImportError("PostgresSink needs psycopg: pip install odata-incremental-sync[postgres]") from exc
        return cls(psycopg.connect(conninfo), table, key_field, **sink_options)
