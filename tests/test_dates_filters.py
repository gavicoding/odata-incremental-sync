from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from odata_sync.dates import day_bounds, format_literal, parse_datetime, require_aware
from odata_sync.filters import chunk_keys, key_filter, literal

BRT = timezone(timedelta(hours=-3))
SP = ZoneInfo("America/Sao_Paulo")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("2026-11-26T10:00:00Z", datetime(2026, 11, 26, 10, tzinfo=timezone.utc)),
        ("2026-11-26T10:00:00-03:00", datetime(2026, 11, 26, 13, tzinfo=timezone.utc)),
        ("2026-11-26T10:00:00.1234567+00:00", datetime(2026, 11, 26, 10, 0, 0, 123456, tzinfo=timezone.utc)),
        ("2026-11-26T10:00:00.5Z", datetime(2026, 11, 26, 10, 0, 0, 500000, tzinfo=timezone.utc)),
        ("2026-11-26 10:00-0300", datetime(2026, 11, 26, 13, tzinfo=timezone.utc)),
        ("/Date(1795694400000)/", datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)),
        ("/Date(1795694400000-0300)/", datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)),
    ],
)
def test_parse_datetime_formats(text, expected):
    assert parse_datetime(text) == expected


def test_ms_date_keeps_the_given_offset():
    assert parse_datetime("/Date(1795694400000-0300)/").utcoffset() == timedelta(hours=-3)


def test_naive_values_need_an_explicit_zone():
    with pytest.raises(ValueError, match="no time zone"):
        parse_datetime("2026-11-26T10:00:00")
    assert parse_datetime("2026-11-26T10:00:00", BRT).utcoffset() == timedelta(hours=-3)
    assert parse_datetime(datetime(2026, 1, 1), timezone.utc).tzinfo is timezone.utc
    assert parse_datetime("2026-11-26", BRT) == datetime(2026, 11, 26, tzinfo=BRT)


@pytest.mark.parametrize("bad", ["ontem", "", 123, "2026-13"])
def test_parse_datetime_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_datetime(bad, timezone.utc)


def test_require_aware():
    with pytest.raises(ValueError, match="explicit time zone"):
        require_aware(datetime(2026, 1, 1))


def test_literal_styles_for_the_same_instant():
    t = datetime(2026, 11, 26, 13, 0, tzinfo=timezone.utc)
    assert format_literal(t, "offset") == "2026-11-26T13:00:00+00:00"
    assert format_literal(t, "offset", BRT) == "2026-11-26T10:00:00-03:00"
    assert format_literal(t, "utc") == "2026-11-26T13:00:00Z"
    assert format_literal(t, "local", BRT) == "2026-11-26T10:00:00"
    assert format_literal(t, "v2", SP) == "datetime'2026-11-26T10:00:00'"
    assert format_literal(t.replace(microsecond=5), "utc") == "2026-11-26T13:00:00.000005Z"


def test_local_style_without_server_tz_is_refused():
    t = datetime(2026, 11, 26, 13, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="needs server_tz"):
        format_literal(t, "local")
    with pytest.raises(ValueError, match="unknown style"):
        format_literal(t, "ticks")
    with pytest.raises(ValueError, match="explicit time zone"):
        format_literal(datetime(2026, 1, 1), "utc")


def test_day_bounds_follow_the_business_zone():
    lo, hi = day_bounds(date(2026, 11, 26), BRT)
    assert lo.isoformat() == "2026-11-26T00:00:00-03:00"
    assert hi - lo == timedelta(days=1)


def test_literal_types():
    assert literal("O'Neil") == "'O''Neil'"
    assert literal(42) == "42"
    assert literal(1.5) == "1.5"
    assert literal(True) == "true"
    assert literal(None) == "null"
    assert literal(datetime(2026, 1, 1, tzinfo=timezone.utc), "utc") == "2026-01-01T00:00:00Z"


def test_key_filter_styles():
    assert key_filter("Key", ["A", "B"]) == "Key in ('A','B')"
    assert key_filter("Key", ["A", "B"], "or") == "(Key eq 'A' or Key eq 'B')"
    assert key_filter("Id", [7], "or") == "Id eq 7"
    with pytest.raises(ValueError):
        key_filter("Key", [])
    with pytest.raises(ValueError):
        key_filter("Key", ["A"], "any")


def test_chunk_by_count():
    chunks = list(chunk_keys("Key", [f"K{i}" for i in range(95)], max_keys=40))
    assert [len(c) for c in chunks] == [40, 40, 15]


def test_chunk_by_filter_length():
    keys = [f"NF{i:06d}" for i in range(30)]
    chunks = list(chunk_keys("Key", keys, max_keys=40, max_chars=100))
    assert all(len(key_filter("Key", c)) <= 100 for c in chunks)
    assert [k for c in chunks for k in c] == keys
    assert len(chunks) > 1


def test_chunk_single_key_too_long():
    with pytest.raises(ValueError, match="single key"):
        list(chunk_keys("Key", ["X" * 50], max_chars=20))
    with pytest.raises(ValueError, match="single key"):
        list(chunk_keys("Key", ["A", "X" * 50], max_chars=20))
    with pytest.raises(ValueError):
        list(chunk_keys("Key", ["A"], max_keys=0))
