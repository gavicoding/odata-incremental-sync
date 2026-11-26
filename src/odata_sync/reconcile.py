"""Key-only reconciliation: list the keys of each day, download only what is missing."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta, timezone, tzinfo

from .client import ODataClient
from .dates import OFFSET, day_bounds
from .filters import chunk_keys, key_filter, literal

log = logging.getLogger("odata_sync")


@dataclass
class DayReport:
    day: date
    remote: int = 0
    present: int = 0
    missing: list[str] = field(default_factory=list)
    fetched: int = 0
    still_missing: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.still_missing and (not self.missing or self.fetched >= len(self.missing))

    def to_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "remote": self.remote,
            "present": self.present,
            "missing": len(self.missing),
            "fetched": self.fetched,
            "still_missing": self.still_missing,
        }


@dataclass
class ReconcileReport:
    entity: str
    days: list[DayReport] = field(default_factory=list)
    requests: int = 0

    @property
    def remote(self) -> int:
        return sum(d.remote for d in self.days)

    @property
    def missing(self) -> int:
        return sum(len(d.missing) for d in self.days)

    @property
    def fetched(self) -> int:
        return sum(d.fetched for d in self.days)

    @property
    def still_missing(self) -> list[str]:
        return [k for d in self.days for k in d.still_missing]

    def to_dict(self) -> dict:
        return {
            "entity": self.entity,
            "remote": self.remote,
            "missing": self.missing,
            "fetched": self.fetched,
            "still_missing": self.still_missing,
            "requests": self.requests,
            "days": [d.to_dict() for d in self.days],
        }

    def table(self) -> str:
        lines = [f"{'day':<10}  {'remote':>6}  {'present':>7}  {'missing':>7}  {'fetched':>7}"]
        for d in self.days:
            lines.append(f"{d.day.isoformat():<10}  {d.remote:>6}  {d.present:>7}  {len(d.missing):>7}  {d.fetched:>7}")
        lines.append(
            f"{'total':<10}  {self.remote:>6}  {self.remote - self.missing:>7}  {self.missing:>7}  {self.fetched:>7}"
        )
        return "\n".join(lines)


def reconcile_by_keys(
    client: ODataClient,
    entity: str,
    key_field: str,
    date_field: str,
    start: date,
    end: date,
    existing_keys: Callable[[list[str]], set[str]],
    fetch_missing: bool = True,
    in_chunk: int = 40,
    sink: Callable[[list[dict]], object] | None = None,
    tz: tzinfo = timezone.utc,
    date_style: str = OFFSET,
    server_tz: tzinfo | None = None,
    page_size: int = 1000,
    in_style: str = "in",
    max_filter_chars: int | None = None,
    extra_filter: str | None = None,
) -> ReconcileReport:
    """For each day from ``start`` to ``end`` (inclusive):

    1. list only the keys on the server (``$select=key``), which is cheap;
    2. ask ``existing_keys(keys)`` which of them you already have;
    3. if ``fetch_missing``, download the full records of the missing ones with
       ``key in (...)`` in chunks of ``in_chunk`` keys (and ``max_filter_chars`` characters),
       passing them to ``sink``.

    Days are cut in ``tz`` (the business calendar), not in UTC.
    """
    if end < start:
        raise ValueError("end must not be before start")
    report = ReconcileReport(entity)
    requests_before = client.requests_made
    day = start
    while day <= end:
        lo, hi = day_bounds(day, tz)
        day_filter = (
            f"{date_field} ge {literal(lo, date_style, server_tz)} and "
            f"{date_field} lt {literal(hi, date_style, server_tz)}"
        )
        if extra_filter:
            day_filter = f"({extra_filter}) and ({day_filter})"
        keys: list[str] = []
        for page in client.iter_pages(
            entity, filter=day_filter, orderby=f"{key_field} asc", select=key_field, page_size=page_size
        ):
            keys.extend(str(r[key_field]) for r in page)
        keys = list(dict.fromkeys(keys))
        have = existing_keys(keys) if keys else set()
        rep = DayReport(day, remote=len(keys), present=len(have & set(keys)))
        rep.missing = [k for k in keys if k not in have]
        if rep.missing and fetch_missing:
            got: set[str] = set()
            for chunk in chunk_keys(key_field, rep.missing, in_chunk, max_filter_chars, in_style):
                flt = key_filter(key_field, chunk, in_style)
                pages = client.iter_pages(entity, filter=flt, orderby=f"{key_field} asc", page_size=max(len(chunk), 1))
                for page in pages:
                    if sink is not None:
                        sink(page)
                    got.update(str(r[key_field]) for r in page)
            rep.fetched = len(got)
            rep.still_missing = [k for k in rep.missing if k not in got]
        log.info("%s %s: %d remote, %d missing, %d fetched", entity, day, rep.remote, len(rep.missing), rep.fetched)
        report.days.append(rep)
        day += timedelta(days=1)
    report.requests = client.requests_made - requests_before
    return report
