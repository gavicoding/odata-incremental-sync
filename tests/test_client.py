from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses

from odata_sync import AuthError, ODataClient, ODataError, RetryExhausted
from odata_sync.client import encode_query, extract_records, retry_after_seconds

BASE = "https://erp.exemplo.test/odata/"


def client(**kw) -> tuple[ODataClient, list[float]]:
    sleeps: list[float] = []
    kw.setdefault("backoff", 1.0)
    return ODataClient(BASE, sleep=sleeps.append, **kw), sleeps


def qs(call) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(call.request.url).query).items()}


def test_encode_query_uses_percent_20_and_keeps_odata_syntax():
    q = encode_query({"$filter": "Name eq 'Ana Maria' and T ge 2026-01-01T00:00:00-03:00", "$top": 10, "x": None})
    assert "+" not in q
    assert "%20" in q
    assert "$filter=" in q and "'Ana%20Maria'" in q
    assert "-03:00" in q
    assert q.endswith("$top=10")
    assert "x=" not in q


def test_encode_query_escapes_plus_in_offsets():
    assert "%2B05:30" in encode_query({"$filter": "T ge 2026-01-01T00:00:00+05:30"})


@responses.activate
def test_skip_paging_until_short_page():
    for skip, n in ((0, 3), (3, 3), (6, 1)):
        responses.get(
            f"{BASE}Items",
            json={"value": [{"Id": skip + i} for i in range(n)]},
            match=[responses.matchers.query_param_matcher({"$top": "3", **({"$skip": str(skip)} if skip else {})})],
        )
    c, _ = client()
    pages = list(c.iter_pages("Items", page_size=3))
    assert [len(p) for p in pages] == [3, 3, 1]
    assert [r["Id"] for p in pages for r in p] == list(range(7))


@responses.activate
def test_exact_multiple_ends_with_an_empty_page():
    responses.get(f"{BASE}Items", json={"value": [{"Id": 1}, {"Id": 2}]})
    responses.get(f"{BASE}Items", json={"value": []})
    c, _ = client()
    assert [len(p) for p in c.iter_pages("Items", page_size=2)] == [2]
    assert len(responses.calls) == 2


@responses.activate
def test_follows_next_link_absolute_and_relative():
    responses.get(f"{BASE}Items", json={"value": [{"Id": 1}], "@odata.nextLink": f"{BASE}Items?$skiptoken=1"})
    responses.get(f"{BASE}Items", json={"value": [{"Id": 2}], "@odata.nextLink": "Items?$skiptoken=2"})
    responses.get(f"{BASE}Items", json={"value": [{"Id": 3}]})
    c, _ = client()
    pages = list(c.iter_pages("Items", page_size=100))
    assert [r["Id"] for p in pages for r in p] == [1, 2, 3]
    assert "$skiptoken=1" in responses.calls[1].request.url
    assert responses.calls[2].request.url == f"{BASE}Items?$skiptoken=2"


@responses.activate
def test_server_paging_page_without_next_link_is_the_last():
    responses.get(f"{BASE}Items", json={"value": [{"Id": 1}, {"Id": 2}], "@odata.nextLink": "Items?$skiptoken=2"})
    responses.get(f"{BASE}Items", json={"value": [{"Id": 3}, {"Id": 4}]})
    c, _ = client()
    assert sum(len(p) for p in c.iter_pages("Items", page_size=2)) == 4
    assert len(responses.calls) == 2  # did not fall back to $skip


@responses.activate
def test_odata_v2_shapes():
    responses.get(f"{BASE}Items", json={"d": {"results": [{"Id": 1}], "__next": f"{BASE}Items?$skiptoken=1"}})
    responses.get(f"{BASE}Items", json={"d": [{"Id": 2}]})
    c, _ = client()
    assert [r["Id"] for r in c.iter_records("Items")] == [1, 2]


def test_extract_records_rejects_unknown_shapes():
    with pytest.raises(ODataError, match="unexpected response"):
        extract_records({"items": []})


@responses.activate
def test_max_top_caps_the_page_size():
    responses.get(f"{BASE}Items", json={"value": []})
    c, _ = client(max_top=100)
    list(c.iter_pages("Items", page_size=5000))
    assert qs(responses.calls[0])["$top"] == "100"


def test_page_size_must_be_positive():
    c, _ = client()
    with pytest.raises(ValueError):
        list(c.iter_pages("Items", page_size=0))


@responses.activate
def test_select_filter_orderby_are_sent():
    responses.get(f"{BASE}Items", json={"value": []})
    c, _ = client()
    list(c.iter_pages("Items", filter="A eq 1", orderby="A asc", select=["A", "B"]))
    q = qs(responses.calls[0])
    assert q["$filter"] == "A eq 1"
    assert q["$orderby"] == "A asc"
    assert q["$select"] == "A,B"


@responses.activate
def test_max_pages():
    responses.get(f"{BASE}Items", json={"value": [{"Id": 1}]})
    c, _ = client()
    assert len(list(c.iter_pages("Items", page_size=1, max_pages=1))) == 1


@responses.activate
def test_retry_on_429_honors_retry_after():
    responses.get(f"{BASE}Items", status=429, headers={"Retry-After": "7"})
    responses.get(f"{BASE}Items", status=503)
    responses.get(f"{BASE}Items", json={"value": [{"Id": 1}]})
    c, sleeps = client(backoff=0.5)
    assert c.get_json("Items")["value"] == [{"Id": 1}]
    assert sleeps == [7.0, 1.0]  # Retry-After, then 0.5 * 2**1


@responses.activate
def test_backoff_is_capped():
    for _ in range(4):
        responses.get(f"{BASE}Items", status=500)
    responses.get(f"{BASE}Items", json={"value": []})
    c, sleeps = client(backoff=10, max_backoff=25)
    c.get_json("Items")
    assert sleeps == [10, 20, 25, 25]


@responses.activate
def test_retry_on_network_errors_then_gives_up():
    for _ in range(3):
        responses.get(f"{BASE}Items", body=requests.ConnectionError("connection reset"))
    c, sleeps = client(max_retries=2)
    with pytest.raises(RetryExhausted, match="3 attempts: ConnectionError"):
        c.get_json("Items")
    assert len(sleeps) == 2


@responses.activate
def test_timeout_is_retried():
    responses.get(f"{BASE}Items", body=requests.Timeout("slow"))
    responses.get(f"{BASE}Items", json={"value": []})
    c, sleeps = client()
    assert c.get_json("Items") == {"value": []}
    assert sleeps == [1.0]


@pytest.mark.parametrize("status", [401, 403])
@responses.activate
def test_auth_errors_are_clear_and_not_retried(status):
    responses.get(f"{BASE}Items", status=status, body="token expired")
    c, sleeps = client(token="segredo-ficticio")
    with pytest.raises(AuthError, match=f"HTTP {status}.*token or credentials") as exc:
        c.get_json("Items?$top=1")
    assert exc.value.status == status
    assert exc.value.body == "token expired"
    assert sleeps == []
    assert responses.calls[0].request.headers["Authorization"] == "Bearer segredo-ficticio"


@responses.activate
def test_other_4xx_is_not_retried():
    responses.get(f"{BASE}Items", status=400, body="bad filter")
    c, sleeps = client()
    with pytest.raises(ODataError, match=r"HTTP 400.*bad filter") as exc:
        c.get_json("Items")
    assert not isinstance(exc.value, RetryExhausted)
    assert sleeps == []


@responses.activate
def test_non_json_response():
    responses.get(f"{BASE}Items", body="<html>login</html>")
    c, _ = client()
    with pytest.raises(ODataError, match="not JSON"):
        c.get_json("Items")


def test_retry_after_parsing():
    now = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc)
    assert retry_after_seconds("12") == 12.0
    assert retry_after_seconds("Thu, 26 Nov 2026 12:00:30 GMT", now) == 30.0
    assert retry_after_seconds("Thu, 26 Nov 2026 11:00:00 GMT", now) == 0.0
    assert retry_after_seconds("soon") is None
    assert retry_after_seconds(None) is None
    later = (now + timedelta(seconds=5)).strftime("%a, %d %b %Y %H:%M:%S")
    assert retry_after_seconds(later, now) == 5.0  # no zone: read as UTC


def test_base_url_gets_trailing_slash_and_custom_headers():
    c = ODataClient("https://erp.exemplo.test/odata", headers={"X-Tenant": "exemplo"})
    assert c.url_for("Items") == "https://erp.exemplo.test/odata/Items"
    assert c.session.headers["X-Tenant"] == "exemplo"
    c2 = ODataClient(BASE, auth=("usuario", "senha-ficticia"))
    assert c2.session.auth == ("usuario", "senha-ficticia")
