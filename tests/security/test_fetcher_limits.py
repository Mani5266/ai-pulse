"""Fetcher limit tests: redirects, response size, and validation on every hop."""

from __future__ import annotations

import ipaddress
from collections.abc import Callable

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import (
    FetchError,
    ResponseTooLargeError,
    TooManyRedirectsError,
    UnsafeURLError,
)
from app.ingestion.fetcher import SafeFetcher
from app.ingestion.urlguard import IpAddress, Resolver

PUBLIC = "93.184.216.34"


def settings(**overrides: object) -> Settings:
    defaults: dict[str, object] = {"_env_file": None}
    defaults.update(overrides)
    return Settings(**defaults)  # type: ignore[arg-type]


def resolver_map(mapping: dict[str, str]) -> Resolver:
    """Resolve each hostname to the address the test assigns it."""

    def resolve(host: str, port: int) -> list[IpAddress]:
        return [ipaddress.ip_address(mapping.get(host, PUBLIC))]

    return resolve


def fetcher_with(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    resolver: Resolver | None = None,
    **setting_overrides: object,
) -> SafeFetcher:
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    return SafeFetcher(
        settings(**setting_overrides),
        client=client,
        resolver=resolver or resolver_map({}),
    )


def test_successful_fetch_returns_the_body() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<rss/>", headers={"content-type": "text/xml"})

    with fetcher_with(handler) as fetcher:
        response = fetcher.get("https://example.com/rss")

    assert response.status_code == 200
    assert response.content == b"<rss/>"
    assert response.content_type == "text/xml"


def test_request_connects_to_the_validated_address_with_the_original_host() -> None:
    """DNS rebinding defence: the socket target is the IP that was checked."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers["host"]))
        return httpx.Response(200, content=b"<rss/>")

    with fetcher_with(handler) as fetcher:
        fetcher.get("https://example.com/rss")

    assert seen == [(PUBLIC, "example.com")]


def test_response_larger_than_the_cap_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 5000)

    with fetcher_with(handler, http_max_response_bytes=1024) as fetcher:
        with pytest.raises(ResponseTooLargeError, match="1024 bytes"):
            fetcher.get("https://example.com/rss")


def test_redirect_chain_is_followed_within_the_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rss":
            return httpx.Response(302, headers={"location": "https://example.com/feed.xml"})
        return httpx.Response(200, content=b"<rss/>")

    with fetcher_with(handler, http_max_redirects=2) as fetcher:
        response = fetcher.get("https://example.com/rss")

    assert response.content == b"<rss/>"
    assert response.url == "https://example.com/feed.xml"


def test_redirect_chain_beyond_the_limit_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://example.com/next"})

    with fetcher_with(handler, http_max_redirects=2) as fetcher:
        with pytest.raises(TooManyRedirectsError, match="exceeded 2 redirects"):
            fetcher.get("https://example.com/rss")


def test_redirect_into_a_private_address_is_rejected() -> None:
    """The whole point of validating every hop, not only the first."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://internal.example.com/admin"})

    resolver = resolver_map({"example.com": PUBLIC, "internal.example.com": "10.0.0.5"})

    with fetcher_with(handler, resolver=resolver) as fetcher:
        with pytest.raises(UnsafeURLError, match="private"):
            fetcher.get("https://example.com/rss")


def test_redirect_to_a_non_http_scheme_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "file:///etc/passwd"})

    with fetcher_with(handler) as fetcher:
        with pytest.raises(UnsafeURLError, match="scheme"):
            fetcher.get("https://example.com/rss")


def test_redirect_without_a_location_header_is_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    with fetcher_with(handler) as fetcher:
        with pytest.raises(FetchError, match="without Location"):
            fetcher.get("https://example.com/rss")


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 503])
def test_error_statuses_raise(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"nope")

    with fetcher_with(handler) as fetcher:
        with pytest.raises(FetchError, match=f"HTTP {status}"):
            fetcher.get("https://example.com/rss")


def test_transport_errors_are_wrapped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("too slow")

    with fetcher_with(handler) as fetcher:
        with pytest.raises(FetchError, match="ConnectTimeout"):
            fetcher.get("https://example.com/rss")
