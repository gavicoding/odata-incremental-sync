"""Building $filter expressions without string-concatenation bugs."""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import datetime, tzinfo

from .dates import OFFSET, format_literal


def literal(value: object, date_style: str = OFFSET, server_tz: tzinfo | None = None) -> str:
    """An OData literal: strings quoted with ``'`` doubled, numbers bare, datetimes per style."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, datetime):
        return format_literal(value, date_style, server_tz)
    if value is None:
        return "null"
    return "'" + str(value).replace("'", "''") + "'"


def key_filter(field: str, keys: Sequence[object], style: str = "in") -> str:
    """``Key in ('a','b')`` (OData 4.01) or ``(Key eq 'a' or Key eq 'b')`` for older servers."""
    if not keys:
        raise ValueError("keys must not be empty")
    values = [literal(k) for k in keys]
    if style == "in":
        return f"{field} in ({','.join(values)})"
    if style == "or":
        inner = " or ".join(f"{field} eq {v}" for v in values)
        return f"({inner})" if len(values) > 1 else inner
    raise ValueError("style must be 'in' or 'or'")


def chunk_keys(
    field: str,
    keys: Sequence[object],
    max_keys: int = 40,
    max_chars: int | None = None,
    style: str = "in",
) -> Iterator[list[object]]:
    """Split keys so each filter has at most ``max_keys`` keys and ``max_chars`` characters.

    Many APIs reject long filters (often with a vague 400 or 414). ``max_chars`` measures the
    filter text before URL encoding.
    """
    if max_keys < 1:
        raise ValueError("max_keys must be at least 1")
    chunk: list[object] = []
    for key in keys:
        candidate = [*chunk, key]
        too_long = max_chars is not None and len(key_filter(field, candidate, style)) > max_chars
        if chunk and (len(candidate) > max_keys or too_long):
            yield chunk
            chunk = [key]
            if max_chars is not None and len(key_filter(field, chunk, style)) > max_chars:
                raise ValueError(f"a single key does not fit in max_chars={max_chars}: {key!r}")
        elif not chunk and too_long:
            raise ValueError(f"a single key does not fit in max_chars={max_chars}: {key!r}")
        else:
            chunk = candidate
    if chunk:
        yield chunk
