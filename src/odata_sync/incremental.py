"""Incremental sync with a checkpoint that only moves after the sink confirms."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo

from .checkpoint import CheckpointStore
from .client import ODataClient
from .dates import OFFSET, parse_datetime, require_aware
from .errors import ODataError
from .filters import literal

log = logging.getLogger("odata_sync")

Sink = Callable[[list[dict]], object]


@dataclass
class SyncResult:
    entity: str
    checkpoint_before: str | None
    checkpoint_after: str | None
    since: datetime | None
    pages: int = 0
    records: int = 0
    requests: int = 0
    page_sizes: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "entity": self.entity,
            "checkpoint_before": self.checkpoint_before,
            "checkpoint_after": self.checkpoint_after,
            "since": self.since.isoformat() if self.since else None,
            "pages": self.pages,
            "records": self.records,
            "requests": self.requests,
        }


def _and(*parts: str | None) -> str | None:
    items = [f"({p})" for p in parts if p]
    return " and ".join(items) if items else None


def sync_incremental(
    client: ODataClient,
    entity: str,
    key_field: str,
    cursor_field: str,
    store: CheckpointStore,
    sink: Sink,
    lookback: timedelta = timedelta(minutes=5),
    start: datetime | None = None,
    page_size: int = 100,
    select: list[str] | None = None,
    extra_filter: str | None = None,
    checkpoint_name: str | None = None,
    date_style: str = OFFSET,
    server_tz: tzinfo | None = None,
    max_pages: int | None = None,
) -> SyncResult:
    """Read every record changed since the last checkpoint and hand it to ``sink`` page by page.

    - The first request asks for ``cursor_field ge (checkpoint - lookback)``. The lookback
      re-reads a small window to catch rows committed late with an older timestamp; the sink's
      upsert makes those repeats harmless.
    - Pages are ordered by ``cursor_field`` and ``key_field`` and walked with keyset paging
      (``cursor gt last or (cursor eq last and key gt last_key)``) instead of ``$skip``. With
      ``$skip``, a row edited during the run jumps to the end, every row behind it shifts one
      position, and one record is silently skipped.
    - After ``sink(page)`` returns, the checkpoint is set to the last cursor of that page. If the
      sink raises, the checkpoint stays where it was and the error propagates.

    ``start`` is used when there is no checkpoint yet; without it the first run reads everything.
    ``server_tz`` is the zone assumed for cursor values the server sends without an offset.
    """
    name = checkpoint_name or entity
    before = store.get(name)
    since: datetime | None = None
    if before is not None:
        since = parse_datetime(before, server_tz or timezone.utc) - lookback
    elif start is not None:
        since = require_aware(start, "start")

    def lit(value: object) -> str:
        return literal(value, date_style, server_tz)

    select_fields = None
    if select:
        select_fields = list(dict.fromkeys([key_field, cursor_field, *select]))
    orderby = f"{cursor_field} asc,{key_field} asc"
    base = _and(extra_filter, f"{cursor_field} ge {lit(since)}" if since else None)

    result = SyncResult(entity, before, before, since)
    requests_before = client.requests_made
    last_cursor_raw: object = None
    last_cursor: datetime | None = None
    last_key: object = None
    while max_pages is None or result.pages < max_pages:
        seek = None
        if last_cursor is not None:
            c, k = lit(last_cursor), lit(last_key)
            seek = f"{cursor_field} gt {c} or ({cursor_field} eq {c} and {key_field} gt {k})"
        records, _ = client.fetch_page(entity, _and(base, seek), orderby, select_fields, page_size)
        if not records:
            break
        last = records[-1]
        new_cursor = parse_datetime(last.get(cursor_field), server_tz or timezone.utc)
        new_key = last.get(key_field)
        if last_cursor is not None and (new_cursor, new_key) == (last_cursor, last_key):
            # Happens when the server keeps more precision than the literal we send back
            # (e.g. 7 fractional digits), so "gt last" matches the same row forever.
            raise ODataError(
                f"keyset paging made no progress at {cursor_field}={last.get(cursor_field)!r}, "
                f"{key_field}={new_key!r}; check the precision and time zone of {cursor_field}"
            )
        sink(records)  # if this raises, nothing below runs and the checkpoint stays put
        last_cursor_raw = last.get(cursor_field)
        last_cursor, last_key = new_cursor, new_key
        store.set(name, last_cursor.isoformat())
        result.checkpoint_after = last_cursor.isoformat()
        result.pages += 1
        result.records += len(records)
        result.page_sizes.append(len(records))
        log.info("%s: page %d, %d records, cursor %s", entity, result.pages, len(records), last_cursor_raw)
    result.requests = client.requests_made - requests_before
    return result
