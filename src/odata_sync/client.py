"""Small OData client: safe paging, retries with backoff and clear auth errors."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import requests

from .errors import AuthError, ODataError, RetryExhausted

log = logging.getLogger("odata_sync")

RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
# Characters OData expects literally in query values. Spaces become %20, never "+".
_SAFE = "$(),'*:@/=;"


def encode_query(params: dict[str, Any]) -> str:
    parts = []
    for name, value in params.items():
        if value is None:
            continue
        parts.append(f"{quote(str(name), safe='$')}={quote(str(value), safe=_SAFE)}")
    return "&".join(parts)


def extract_records(payload: Any) -> list[dict]:
    """Records of a page in OData v4 (``value``) or v2 (``d.results`` / ``d``) format."""
    if isinstance(payload, dict):
        if isinstance(payload.get("value"), list):
            return payload["value"]
        d = payload.get("d")
        if isinstance(d, dict) and isinstance(d.get("results"), list):
            return d["results"]
        if isinstance(d, list):
            return d
    raise ODataError(f"unexpected response shape: {str(payload)[:200]}")


def extract_next_link(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    link = payload.get("@odata.nextLink") or payload.get("odata.nextLink")
    d = payload.get("d")
    if not link and isinstance(d, dict):
        link = d.get("__next")
    return link or None


def retry_after_seconds(header: str | None, now: datetime | None = None) -> float | None:
    """Seconds to wait according to a Retry-After header (delta seconds or HTTP date)."""
    if not header:
        return None
    header = header.strip()
    if header.isdigit():
        return float(header)
    try:
        when = parsedate_to_datetime(header)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    now = now or datetime.now(timezone.utc)
    return max(0.0, (when - now).total_seconds())


class ODataClient:
    """
    Parameters
    ----------
    base_url: service root, e.g. ``https://erp.exemplo.test/odata/v4/``.
    token: bearer token. Use ``auth`` instead for basic auth or any ``requests`` auth object.
    timeout: seconds for each request.
    max_retries: retries after the first attempt for 429, 5xx and network errors.
    backoff: base delay; attempt ``n`` waits ``backoff * 2**n`` seconds (capped by
        ``max_backoff``) unless the server sends ``Retry-After``.
    max_top: largest ``$top`` the server accepts. Bigger page sizes are reduced to it.
    """

    def __init__(
        self,
        base_url: str,
        token: str | None = None,
        auth: Any = None,
        timeout: float = 30.0,
        max_retries: int = 5,
        backoff: float = 1.0,
        max_backoff: float = 60.0,
        max_top: int | None = None,
        headers: dict[str, str] | None = None,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff = backoff
        self.max_backoff = max_backoff
        self.max_top = max_top
        self.session = session or requests.Session()
        self.session.headers.setdefault("Accept", "application/json")
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        if auth is not None:
            self.session.auth = auth
        if headers:
            self.session.headers.update(headers)
        self._sleep = sleep
        self.requests_made = 0

    # -- low level -------------------------------------------------------------------------

    def url_for(self, entity: str, params: dict[str, Any] | None = None) -> str:
        url = urljoin(self.base_url, entity)
        query = encode_query(params or {})
        return f"{url}?{query}" if query else url

    def get_json(self, url: str) -> Any:
        """GET with retries. ``url`` may be absolute or relative to ``base_url``."""
        url = urljoin(self.base_url, url)
        last_error = ""
        for attempt in range(self.max_retries + 1):
            wait: float | None = None
            try:
                self.requests_made += 1
                response = self.session.get(url, timeout=self.timeout)
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                status = response.status_code
                if status in (401, 403):
                    raise AuthError(
                        f"HTTP {status} from {url.split('?')[0]}: check the token or credentials "
                        "and whether this user may read the entity",
                        status,
                        response.text[:500],
                    )
                if status in RETRY_STATUS:
                    last_error = f"HTTP {status}"
                    wait = retry_after_seconds(response.headers.get("Retry-After"))
                elif status >= 400:
                    raise ODataError(
                        f"HTTP {status} from {url.split('?')[0]}: {response.text[:300]}",
                        status,
                        response.text[:2000],
                    )
                else:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise ODataError(f"response is not JSON: {response.text[:200]}", status) from exc
            if attempt == self.max_retries:
                break
            if wait is None:
                wait = min(self.max_backoff, self.backoff * (2**attempt))
            log.warning("retry %d/%d in %.1fs after %s", attempt + 1, self.max_retries, wait, last_error)
            self._sleep(wait)
        raise RetryExhausted(f"giving up after {self.max_retries + 1} attempts: {last_error}")

    def _top(self, page_size: int) -> int:
        if page_size < 1:
            raise ValueError("page_size must be at least 1")
        return min(page_size, self.max_top) if self.max_top else page_size

    def _checked_next_link(self, link: str) -> str:
        """Resolve a nextLink and refuse one that points to another host.

        The session carries the credentials, so following a link to a different scheme or host
        would send the token there.
        """
        url = urljoin(self.base_url, link)
        base, target = urlsplit(self.base_url), urlsplit(url)
        if (target.scheme.lower(), target.netloc.lower()) != (base.scheme.lower(), base.netloc.lower()):
            raise ODataError(
                f"nextLink points to {target.scheme}://{target.netloc}, outside {base.scheme}://{base.netloc}; "
                "not following it so the credentials are not sent to another host"
            )
        return url

    # -- paging ----------------------------------------------------------------------------

    def fetch_page(
        self,
        entity: str,
        filter: str | None = None,
        orderby: str | None = None,
        select: str | list[str] | None = None,
        top: int = 100,
        skip: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> tuple[list[dict], str | None]:
        """One page: (records, nextLink or None)."""
        params: dict[str, Any] = {
            "$filter": filter,
            "$orderby": orderby,
            "$select": ",".join(select) if isinstance(select, list) else select,
            "$top": self._top(top),
            "$skip": skip or None,
            **(extra or {}),
        }
        payload = self.get_json(self.url_for(entity, params))
        return extract_records(payload), extract_next_link(payload)

    def iter_pages(
        self,
        entity: str,
        filter: str | None = None,
        orderby: str | None = None,
        select: str | list[str] | None = None,
        page_size: int = 100,
        extra: dict[str, Any] | None = None,
        max_pages: int | None = None,
    ) -> Iterator[list[dict]]:
        """Yield each page of records.

        Follows ``@odata.nextLink`` when the server sends it (server-driven paging). Otherwise
        pages with ``$top``/``$skip`` and stops at the first page shorter than ``$top``.
        Always pass ``orderby`` with a unique key, or ``$skip`` paging can repeat or miss rows.
        """
        top = self._top(page_size)
        records, next_link = self.fetch_page(entity, filter, orderby, select, top, None, extra)
        skip = 0
        pages = 0
        server_paging = False
        seen_links: set[str] = set()
        while True:
            if records:
                pages += 1
                yield records
            if max_pages is not None and pages >= max_pages:
                return
            if next_link:
                server_paging = True
                next_url = self._checked_next_link(next_link)
                if next_url in seen_links:
                    raise ODataError(f"server repeated the same nextLink, stopping to avoid a loop: {next_url}")
                seen_links.add(next_url)
                payload = self.get_json(next_url)
                records, next_link = extract_records(payload), extract_next_link(payload)
                continue
            # Once the server drives paging, a page without nextLink is the last one.
            if server_paging or len(records) < top:
                return
            skip += len(records)
            records, next_link = self.fetch_page(entity, filter, orderby, select, top, skip, extra)

    def iter_records(self, entity: str, **kwargs: Any) -> Iterator[dict]:
        for page in self.iter_pages(entity, **kwargs):
            yield from page
