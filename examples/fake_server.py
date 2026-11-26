"""A small fake OData v4 server for demos and tests. Standard library only.

It serves generated invoices (all fictional) and understands enough of OData to exercise a real
client: $filter (eq ne gt ge lt le, and or not, parentheses, in), $orderby, $select, $top,
$skip, server-driven paging with @odata.nextLink, an optional bearer token and injected faults
(429 with Retry-After, 503).

    python examples/fake_server.py --port 8080 --records 500
    curl "http://127.0.0.1:8080/odata/Invoices?\\$top=2"
"""

from __future__ import annotations

import argparse
import json
import random
import re
import threading
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone, tzinfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

LOCAL_TZ = timezone(timedelta(hours=-3), "UTC-03")

# --------------------------------------------------------------------------------------------
# Data


def iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def generate_invoices(
    count: int = 300,
    start: date = date(2026, 11, 1),
    days: int = 10,
    seed: int = 42,
    tz: tzinfo = LOCAL_TZ,
) -> list[dict[str, Any]]:
    """Deterministic fictional invoices spread over ``days`` days."""
    rng = random.Random(seed)
    customers = [f"Cliente Exemplo {i:02d}" for i in range(1, 21)]
    records = []
    for i in range(1, count + 1):
        day = start + timedelta(days=rng.randrange(days))
        issued = datetime(day.year, day.month, day.day, rng.randrange(7, 22), rng.randrange(60), tzinfo=tz)
        modified = issued + timedelta(minutes=rng.randrange(0, 90))
        records.append(
            {
                "Key": f"NF{i:06d}",
                "IssuedAt": iso(issued),
                "ModifiedAt": iso(modified),
                "Customer": rng.choice(customers),
                "Total": round(rng.uniform(10, 5000), 2),
                "Status": "authorized",
            }
        )
    return records


# --------------------------------------------------------------------------------------------
# $filter parsing

_TOKEN = re.compile(
    r"\s*(?:"
    r"(?P<dtv2>datetime'[^']*')"
    r"|(?P<str>'(?:[^']|'')*')"
    r"|(?P<dt>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)"
    r"|(?P<num>-?\d+(?:\.\d+)?)"
    r"|(?P<punc>[(),])"
    r"|(?P<word>[A-Za-z_][A-Za-z0-9_/]*)"
    r")"
)


class FilterError(ValueError):
    pass


def _parse_dt(text: str, assume: tzinfo) -> datetime:
    text = text.replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    return dt if dt.tzinfo else dt.replace(tzinfo=assume)


def tokenize(text: str) -> list[tuple[str, Any]]:
    tokens, pos = [], 0
    text = text.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise FilterError(f"cannot parse $filter near: {text[pos : pos + 20]!r}")
        pos = m.end()
        kind = m.lastgroup
        value = m.group(kind)
        if kind == "str":
            tokens.append(("lit", value[1:-1].replace("''", "'")))
        elif kind == "dt":
            tokens.append(("dt", value))
        elif kind == "dtv2":
            tokens.append(("dt", value[len("datetime'") : -1]))
        elif kind == "num":
            tokens.append(("lit", float(value) if "." in value else int(value)))
        elif kind == "punc":
            tokens.append((value, value))
        else:
            low = value.lower()
            if low in ("and", "or", "not", "eq", "ne", "gt", "ge", "lt", "le", "in"):
                tokens.append((low, low))
            elif low in ("true", "false"):
                tokens.append(("lit", low == "true"))
            elif low == "null":
                tokens.append(("lit", None))
            else:
                tokens.append(("ident", value))
    return tokens


Predicate = Callable[[dict], bool]
_OPS = {
    "eq": lambda a, b: a == b,
    "ne": lambda a, b: a != b,
    "gt": lambda a, b: a is not None and b is not None and a > b,
    "ge": lambda a, b: a is not None and b is not None and a >= b,
    "lt": lambda a, b: a is not None and b is not None and a < b,
    "le": lambda a, b: a is not None and b is not None and a <= b,
}


def parse_filter(text: str, tz: tzinfo = LOCAL_TZ) -> Predicate:
    """Compile a $filter string into a predicate over a record."""
    tokens = tokenize(text)
    pos = 0

    def peek(kind: str | None = None) -> bool:
        return pos < len(tokens) and (kind is None or tokens[pos][0] == kind)

    def take(kind: str) -> Any:
        nonlocal pos
        if not peek(kind):
            got = tokens[pos][1] if pos < len(tokens) else "end"
            raise FilterError(f"expected {kind}, got {got!r}")
        pos += 1
        return tokens[pos - 1][1]

    def value() -> tuple[str, Any]:
        nonlocal pos
        if peek("lit") or peek("dt"):
            pos += 1
            return tokens[pos - 1]
        raise FilterError("expected a literal")

    def field_value(record: dict, name: str, kind: str) -> Any:
        raw = record.get(name)
        if kind == "dt" and isinstance(raw, str):
            return _parse_dt(raw, tz)
        return raw

    def convert(kind: str, lit: Any) -> Any:
        return _parse_dt(lit, tz) if kind == "dt" else lit

    def comparison() -> Predicate:
        name = take("ident")
        if peek("in"):
            take("in")
            take("(")
            items = [value()]
            while peek(","):
                take(",")
                items.append(value())
            take(")")
            kinds = {k for k, _ in items}
            kind = "dt" if "dt" in kinds else "lit"
            options = {convert(k, v) for k, v in items}
            return lambda r: field_value(r, name, kind) in options
        for op in _OPS:
            if peek(op):
                take(op)
                kind, lit = value()
                target = convert(kind, lit)
                fn = _OPS[op]
                return lambda r: fn(field_value(r, name, kind), target)
        raise FilterError(f"expected an operator after {name!r}")

    def primary() -> Predicate:
        if peek("("):
            take("(")
            inner = expr()
            take(")")
            return inner
        if peek("not"):
            take("not")
            inner = primary()
            return lambda r: not inner(r)
        return comparison()

    def conj() -> Predicate:
        parts = [primary()]
        while peek("and"):
            take("and")
            parts.append(primary())
        return parts[0] if len(parts) == 1 else (lambda r: all(p(r) for p in parts))

    def expr() -> Predicate:
        parts = [conj()]
        while peek("or"):
            take("or")
            parts.append(conj())
        return parts[0] if len(parts) == 1 else (lambda r: any(p(r) for p in parts))

    predicate = expr()
    if pos != len(tokens):
        raise FilterError(f"unexpected {tokens[pos][1]!r} in $filter")
    return predicate


def sort_records(records: list[dict], orderby: str, tz: tzinfo = LOCAL_TZ) -> list[dict]:
    result = list(records)
    for part in reversed([p.strip() for p in orderby.split(",") if p.strip()]):
        bits = part.split()
        name, desc = bits[0], len(bits) > 1 and bits[1].lower() == "desc"

        def key(r: dict, name: str = name) -> tuple:
            v = r.get(name)
            if isinstance(v, str) and re.match(r"^\d{4}-\d{2}-\d{2}T", v):
                v = _parse_dt(v, tz)
            return (v is None, v)

        result.sort(key=key, reverse=desc)
    return result


# --------------------------------------------------------------------------------------------
# Service


class FakeODataService:
    def __init__(
        self,
        entities: dict[str, list[dict]] | None = None,
        page_cap: int | None = None,
        max_top: int | None = None,
        token: str | None = None,
        tz: tzinfo = LOCAL_TZ,
    ) -> None:
        self.entities = entities if entities is not None else {"Invoices": generate_invoices()}
        self.page_cap = page_cap  # server-driven paging: never more than this per response
        self.max_top = max_top  # reject larger $top with 400, like some ERPs do
        self.token = token
        self.tz = tz
        self.faults: list[tuple[int, str | None]] = []
        self.log: list[str] = []
        self.lock = threading.Lock()

    def fail_next(self, status: int = 429, times: int = 1, retry_after: str | None = "0") -> None:
        with self.lock:
            self.faults.extend([(status, retry_after)] * times)

    def touch(self, entity: str, keys: list[str], when: datetime) -> None:
        """Simulate edits on the server: bump ModifiedAt of the given keys."""
        with self.lock:
            wanted = set(keys)
            for r in self.entities[entity]:
                if r["Key"] in wanted:
                    r["ModifiedAt"] = iso(when.astimezone(self.tz))

    def handle(self, path_qs: str, headers: dict[str, str]) -> tuple[int, dict[str, str], Any]:
        with self.lock:
            self.log.append(path_qs)
            if self.faults:
                status, retry_after = self.faults.pop(0)
                extra = {"Retry-After": retry_after} if retry_after is not None else {}
                return status, extra, {"error": {"message": "simulated failure"}}
        if self.token and headers.get("authorization") != f"Bearer {self.token}":
            return 401, {}, {"error": {"message": "invalid or missing token"}}
        parts = urlsplit(path_qs)
        segments = [s for s in parts.path.split("/") if s]
        if len(segments) != 2 or segments[0] != "odata" or segments[1] not in self.entities:
            return 404, {}, {"error": {"message": f"unknown resource {parts.path}"}}
        entity = segments[1]
        params = dict(parse_qsl(parts.query, keep_blank_values=True))
        try:
            rows = list(self.entities[entity])
            if params.get("$filter"):
                predicate = parse_filter(params["$filter"], self.tz)
                rows = [r for r in rows if predicate(r)]
            if params.get("$orderby"):
                rows = sort_records(rows, params["$orderby"], self.tz)
            skip = int(params.get("$skip") or params.get("$skiptoken") or 0)
            top = int(params["$top"]) if params.get("$top") else None
        except (FilterError, ValueError) as exc:
            return 400, {}, {"error": {"message": str(exc)}}
        if top is not None and self.max_top and top > self.max_top:
            return 400, {}, {"error": {"message": f"$top above the limit of {self.max_top}"}}
        wanted = len(rows) - skip if top is None else top
        size = min(wanted, self.page_cap) if self.page_cap else wanted
        page = rows[skip : skip + max(size, 0)]
        if params.get("$select"):
            fields = [f.strip() for f in params["$select"].split(",")]
            page = [{f: r.get(f) for f in fields} for r in page]
        body: dict[str, Any] = {"@odata.context": f"$metadata#{entity}", "value": page}
        remaining = wanted - len(page)
        if self.page_cap and remaining > 0 and skip + len(page) < len(rows):
            nxt = {k: v for k, v in params.items() if k not in ("$skip", "$skiptoken", "$top")}
            nxt["$skiptoken"] = str(skip + len(page))
            if top is not None:
                nxt["$top"] = str(remaining)
            body["@odata.nextLink"] = f"/odata/{entity}?{urlencode(nxt)}"
        return 200, {}, body


class _Handler(BaseHTTPRequestHandler):
    service: FakeODataService

    def do_GET(self) -> None:
        headers = {k.lower(): v for k, v in self.headers.items()}
        status, extra, body = self.service.handle(self.path, headers)
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        for k, v in extra.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args: Any) -> None:
        pass


def serve(service: FakeODataService, port: int = 0, host: str = "127.0.0.1"):
    """Start the server in a background thread. Returns (server, base_url)."""
    handler = type("Handler", (_Handler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://{host}:{server.server_address[1]}/odata/"


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake OData server with fictional invoices")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--records", type=int, default=300)
    parser.add_argument("--page-cap", type=int, default=None, help="server-driven page size")
    args = parser.parse_args()
    service = FakeODataService({"Invoices": generate_invoices(args.records)}, page_cap=args.page_cap)
    server, url = serve(service, args.port)
    print(f"serving {args.records} fictional invoices at {url}Invoices (Ctrl+C to stop)")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
