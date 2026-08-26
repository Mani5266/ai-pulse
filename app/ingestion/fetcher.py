"""HTTP fetching with hard limits.

Every request in this project goes through :class:`SafeFetcher`. It enforces four things
that a bare ``httpx.get`` does not:

1. **SSRF validation on every hop.** The initial URL and each redirect target are
   validated by :mod:`app.ingestion.urlguard` before a socket is opened.
2. **Connection to the validated address.** The socket is opened against the IP address
   that was checked, with the original hostname supplied for TLS SNI, certificate
   verification and the ``Host`` header. Re-resolving the name would reopen the DNS
   rebinding window the validation exists to close.
3. **A streaming size cap.** The body is read in chunks and abandoned the moment it
   exceeds the limit, so a feed that returns a multi-gigabyte response cannot exhaust
   memory.
4. **Connect and read timeouts.** No request can hang the daily run.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx

from app.core.config import Settings
from app.core.errors import (
    FetchError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UnsafeURLError,
)
from app.ingestion.urlguard import Resolver, ValidatedTarget, validate_url

logger = logging.getLogger(__name__)

USER_AGENT = "ai-pulse/0.1 (+https://github.com/Mani5266/ai-pulse)"

REDIRECT_STATUS_CODES: frozenset[int] = frozenset({301, 302, 303, 307, 308})

CHUNK_SIZE = 64 * 1024


@dataclass(frozen=True, slots=True)
class FetchResponse:
    """A successfully fetched body, with the URL it was finally served from."""

    url: str
    status_code: int
    content: bytes
    content_type: str | None

    @property
    def size_bytes(self) -> int:
        return len(self.content)


def _pinned_url(target: ValidatedTarget) -> str:
    """Rewrite a URL so its host is the validated IP literal.

    The hostname is preserved separately, for SNI, certificate verification and the
    ``Host`` header, so this changes only which address the socket connects to.
    """
    address = target.connect_address
    literal = f"[{address}]" if isinstance(address, ipaddress.IPv6Address) else str(address)
    parts = urlsplit(target.url)
    netloc = f"{literal}:{target.port}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, ""))


class SafeFetcher:
    """Fetches URLs under the limits described in the module docstring.

    Use as a context manager so the underlying connection pool is closed::

        with SafeFetcher(settings) as fetcher:
            response = fetcher.get(url)
    """

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        self._settings = settings
        self._resolver = resolver
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(
                connect=settings.http_connect_timeout,
                read=settings.http_read_timeout,
                write=settings.http_read_timeout,
                pool=settings.http_connect_timeout,
            ),
            follow_redirects=False,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
        )

    def __enter__(self) -> SafeFetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def get(self, url: str) -> FetchResponse:
        """Fetch ``url``, following redirects manually and validating every hop.

        Raises a subclass of :class:`~app.core.errors.FetchError` on any failure. The
        caller is expected to treat that as "skip this source", not "abort the run".
        """
        current = url
        redirects_left = self._settings.http_max_redirects

        while True:
            target = validate_url(current, resolver=self._resolver)
            response = self._request(target)

            if response.status_code in REDIRECT_STATUS_CODES:
                location = response.headers.get("location")
                if not location:
                    raise FetchError(f"{current}: {response.status_code} without Location header")
                if redirects_left <= 0:
                    raise TooManyRedirectsError(
                        f"{url}: exceeded {self._settings.http_max_redirects} redirects"
                    )
                redirects_left -= 1
                next_url = urljoin(current, location)
                logger.debug("redirect %s -> %s", current, next_url)
                response.close()
                current = next_url
                continue

            try:
                if response.status_code >= 400:
                    raise FetchError(f"{current}: HTTP {response.status_code}")
                body = self._read_capped(response, current)
            finally:
                response.close()

            return FetchResponse(
                url=current,
                status_code=response.status_code,
                content=body,
                content_type=response.headers.get("content-type"),
            )

    def _request(self, target: ValidatedTarget) -> httpx.Response:
        """Send one request to a validated target, without following redirects."""
        try:
            request = self._client.build_request(
                "GET",
                _pinned_url(target),
                headers={"Host": target.host},
                extensions={"sni_hostname": target.host},
            )
            return self._client.send(request, stream=True)
        except httpx.HTTPError as exc:
            raise FetchError(f"{target.url}: {type(exc).__name__}: {exc}") from exc

    def _read_capped(self, response: httpx.Response, url: str) -> bytes:
        """Read the body, aborting as soon as the size limit is passed."""
        limit = self._settings.http_max_response_bytes
        chunks: list[bytes] = []
        total = 0

        try:
            for chunk in response.iter_bytes(CHUNK_SIZE):
                total += len(chunk)
                if total > limit:
                    raise ResponseTooLargeError(f"{url}: response exceeded {limit} bytes")
                chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise FetchError(f"{url}: {type(exc).__name__}: {exc}") from exc

        return b"".join(chunks)


__all__ = [
    "FetchError",
    "FetchResponse",
    "ResponseTooLargeError",
    "SafeFetcher",
    "TooManyRedirectsError",
    "UnsafeURLError",
]
