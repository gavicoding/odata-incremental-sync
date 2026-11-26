"""Date handling with explicit time zones.

The classic bug: the server stores ``ModifiedAt`` in local time (say UTC-3) and compares the
filter literally. Sending ``2026-11-26T10:00:00Z`` then means 07:00 local, three hours too
early, or worse, the server ignores the ``Z`` and you skip three hours of changes. This module
never accepts a naive datetime and lets you say exactly how the server wants the literal.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone, tzinfo

# How the datetime literal is written in $filter.
OFFSET = "offset"  # OData v4 with offset: 2026-11-26T10:00:00-03:00
UTC = "utc"  # OData v4 in UTC: 2026-11-26T13:00:00Z
LOCAL = "local"  # no offset, converted to server_tz: 2026-11-26T10:00:00
V2 = "v2"  # OData v2 Edm.DateTime: datetime'2026-11-26T10:00:00' (server_tz, no offset)
STYLES = (OFFSET, UTC, LOCAL, V2)

_ISO = re.compile(
    r"^(\d{4})-(\d{2})-(\d{2})"
    r"(?:[T ](\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?)?"
    r"\s*(Z|[+-]\d{2}:?\d{2})?$",
    re.IGNORECASE,
)
_MS_DATE = re.compile(r"^/Date\((-?\d+)([+-]\d{4})?\)/$")


def require_aware(value: datetime, name: str = "datetime") -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(
            f"{name} must have an explicit time zone (got naive {value.isoformat()}); "
            "use datetime(..., tzinfo=...) or pass server_tz"
        )
    return value


def parse_datetime(value: object, assume_tz: tzinfo | None = None) -> datetime:
    """Parse what OData servers send: ISO 8601 (any fraction length, ``Z`` or offset),
    OData v2 ``/Date(ms)/`` and ``datetime`` objects.

    A value without an offset is interpreted in ``assume_tz``. If ``assume_tz`` is None such a
    value raises ``ValueError``, so a missing time zone never goes unnoticed.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        text = value.strip()
        ms = _MS_DATE.match(text)
        if ms:
            dt = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=int(ms.group(1)))
            if ms.group(2):
                sign = 1 if ms.group(2)[0] == "+" else -1
                off = timedelta(hours=int(ms.group(2)[1:3]), minutes=int(ms.group(2)[3:5])) * sign
                dt = dt.astimezone(timezone(off))
            return dt
        m = _ISO.match(text)
        if not m:
            raise ValueError(f"not a date/time: {value!r}")
        year, month, day, hour, minute, second, frac, tz = m.groups()
        micro = int((frac or "0")[:6].ljust(6, "0"))
        dt = datetime(int(year), int(month), int(day), int(hour or 0), int(minute or 0), int(second or 0), micro)
        if tz:
            if tz.upper() == "Z":
                dt = dt.replace(tzinfo=timezone.utc)
            else:
                digits = tz.replace(":", "")
                sign = 1 if digits[0] == "+" else -1
                off = timedelta(hours=int(digits[1:3]), minutes=int(digits[3:5])) * sign
                dt = dt.replace(tzinfo=timezone(off))
    else:
        raise ValueError(f"not a date/time: {value!r}")
    if dt.tzinfo is None:
        if assume_tz is None:
            raise ValueError(f"{value!r} has no time zone and no assume_tz was given")
        dt = dt.replace(tzinfo=assume_tz)
    return dt


def _iso_seconds(dt: datetime) -> str:
    if dt.microsecond:
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond:06d}"
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def format_literal(value: datetime, style: str = OFFSET, server_tz: tzinfo | None = None) -> str:
    """Write ``value`` as a $filter literal in the given style."""
    require_aware(value)
    if style == OFFSET:
        target = value.astimezone(server_tz) if server_tz else value
        off = target.strftime("%z")
        return f"{_iso_seconds(target)}{off[:3]}:{off[3:]}"
    if style == UTC:
        return _iso_seconds(value.astimezone(timezone.utc)) + "Z"
    if style in (LOCAL, V2):
        if server_tz is None:
            raise ValueError(f"style {style!r} needs server_tz: the server compares in its local time")
        text = _iso_seconds(value.astimezone(server_tz))
        return f"datetime'{text}'" if style == V2 else text
    raise ValueError(f"unknown style {style!r}; use one of {', '.join(STYLES)}")


def day_bounds(day: date, tz: tzinfo) -> tuple[datetime, datetime]:
    """Start of ``day`` and start of the next day, both in ``tz``."""
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    nxt = day + timedelta(days=1)
    return start, datetime(nxt.year, nxt.month, nxt.day, tzinfo=tz)
