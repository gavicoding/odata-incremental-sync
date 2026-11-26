"""Where the last confirmed cursor value is kept between runs."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from .sql import Dialect, get_dialect


class CheckpointStore(ABC):
    """Maps a checkpoint name (usually the entity) to the last cursor value, as text."""

    @abstractmethod
    def get(self, name: str) -> str | None: ...

    @abstractmethod
    def set(self, name: str, value: str) -> None: ...


class MemoryCheckpoint(CheckpointStore):
    """For tests and one-off runs."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self.values = dict(initial or {})

    def get(self, name: str) -> str | None:
        return self.values.get(name)

    def set(self, name: str, value: str) -> None:
        self.values[name] = value


class JsonFileCheckpoint(CheckpointStore):
    """A JSON file ``{"name": "value"}``. Writes are atomic (temp file + rename), so a crash in
    the middle never leaves a half-written checkpoint. Works on a mounted Cloud Storage bucket."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def _read(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"{self.path} must contain a JSON object")
        return data

    def get(self, name: str) -> str | None:
        return self._read().get(name)

    def set(self, name: str, value: str) -> None:
        data = self._read()
        data[name] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".checkpoint-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, sort_keys=True)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


class SqlCheckpoint(CheckpointStore):
    """Checkpoint table in any DB-API connection (SQLite, MySQL, PostgreSQL, Cloud SQL).

    Keeping the checkpoint in the same database as the data lets you commit both together.
    """

    def __init__(self, connection: Any, dialect: str | Dialect = "sqlite", table: str = "odata_checkpoints") -> None:
        self.conn = connection
        self.d = get_dialect(dialect)
        self.table = table
        cur = self.conn.cursor()
        cur.execute(self.d.create_table(table, ["name", "value"], "name"))
        self.conn.commit()

    def get(self, name: str) -> str | None:
        cur = self.conn.cursor()
        cur.execute(
            f"SELECT {self.d.ident('value')} FROM {self.d.ident(self.table)} "
            f"WHERE {self.d.ident('name')} = {self.d.placeholder}",
            (name,),
        )
        row = cur.fetchone()
        self.conn.commit()  # end the read transaction (PostgreSQL keeps it open otherwise)
        return row[0] if row else None

    def set(self, name: str, value: str) -> None:
        cur = self.conn.cursor()
        cur.execute(self.d.upsert(self.table, ["name", "value"], "name"), (name, value))
        self.conn.commit()


class SqliteCheckpoint(SqlCheckpoint):
    """Checkpoint table in a SQLite file."""

    def __init__(self, path: str | os.PathLike[str], table: str = "odata_checkpoints") -> None:
        super().__init__(sqlite3.connect(str(path)), "sqlite", table)

    def close(self) -> None:
        self.conn.close()
