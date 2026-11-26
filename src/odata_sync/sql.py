"""SQL dialect details shared by sinks and checkpoint stores."""

from __future__ import annotations

import re
from dataclasses import dataclass

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class Dialect:
    name: str
    placeholder: str
    quote_char: str
    key_type: str  # type for primary-key text columns
    text_type: str  # type for long text columns

    def ident(self, name: str) -> str:
        """Quote an identifier after checking it, so table/column names can't inject SQL."""
        if not _IDENT.match(name):
            raise ValueError(f"invalid SQL identifier: {name!r} (letters, digits and _ only)")
        return f"{self.quote_char}{name}{self.quote_char}"

    def params(self, n: int) -> str:
        return ", ".join([self.placeholder] * n)

    def upsert(self, table: str, columns: list[str], key: str) -> str:
        """INSERT that updates the row when the key already exists."""
        cols = ", ".join(self.ident(c) for c in columns)
        others = [c for c in columns if c != key]
        head = f"INSERT INTO {self.ident(table)} ({cols}) VALUES ({self.params(len(columns))})"
        if self.name == "mysql":
            if not others:
                return head.replace("INSERT", "INSERT IGNORE", 1)
            sets = ", ".join(f"{self.ident(c)} = VALUES({self.ident(c)})" for c in others)
            return f"{head} ON DUPLICATE KEY UPDATE {sets}"
        if not others:
            return f"{head} ON CONFLICT ({self.ident(key)}) DO NOTHING"
        sets = ", ".join(f"{self.ident(c)} = excluded.{self.ident(c)}" for c in others)
        return f"{head} ON CONFLICT ({self.ident(key)}) DO UPDATE SET {sets}"

    def create_table(self, table: str, columns: list[str], key: str) -> str:
        defs = [
            f"{self.ident(c)} {self.key_type + ' NOT NULL PRIMARY KEY' if c == key else self.text_type}"
            for c in columns
        ]
        return f"CREATE TABLE IF NOT EXISTS {self.ident(table)} ({', '.join(defs)})"


SQLITE = Dialect("sqlite", "?", '"', "TEXT", "TEXT")
MYSQL = Dialect("mysql", "%s", "`", "VARCHAR(255)", "LONGTEXT")
POSTGRES = Dialect("postgres", "%s", '"', "TEXT", "TEXT")
DIALECTS = {d.name: d for d in (SQLITE, MYSQL, POSTGRES)}


def get_dialect(name: str | Dialect) -> Dialect:
    if isinstance(name, Dialect):
        return name
    try:
        return DIALECTS[name]
    except KeyError:
        raise ValueError(f"unknown dialect {name!r}; use sqlite, mysql or postgres") from None
