"""SSRF guard tests.

These are the tests that matter most in this project: the guard is the only thing
standing between a redirect from an untrusted feed and the loopback interface or a cloud
metadata endpoint.
"""

from __future__ import annotations

import ipaddress

import pytest

from app.core.errors import UnsafeURLError
from app.ingestion.urlguard import (
    IpAddress,
    Resolver,
    is_safe_address,
    rejection_reason,
    validate_url,
)


def resolver_for(*addresses: str) -> Resolver:
    """A stand-in resolver so these tests never touch DNS."""

    def resolve(host: str, port: int) -> list[IpAddress]:
        return [ipaddress.ip_address(address) for address in addresses]

    return resolve


PUBLIC = resolver_for("93.184.216.34")


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "127.1.2.3",  # the whole 127/8 range, not only .1
        "0.0.0.0",  # unspecified
        "10.0.0.1",  # RFC1918
        "172.16.5.4",  # RFC1918
        "192.168.1.1",  # RFC1918
        "169.254.169.254",  # cloud instance metadata
        "169.254.1.1",  # link-local generally
        "100.64.0.1",  # carrier-grade NAT
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "::1",  # IPv6 loopback
        "fe80::1",  # IPv6 link-local
        "fc00::1",  # IPv6 unique local
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "::ffff:169.254.169.254",  # IPv4-mapped metadata address
    ],
)
def test_dangerous_addresses_are_rejected(address: str) -> None:
    assert is_safe_address(ipaddress.ip_address(address)) is False
    assert rejection_reason(ipaddress.ip_address(address)) is not None


@pytest.mark.parametrize("address", ["93.184.216.34", "1.1.1.1", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_safe_address(ipaddress.ip_address(address)) is True


def test_a_public_hostname_resolving_to_loopback_is_rejected() -> None:
    """The check is on the resolved address, so a friendly hostname is no defence."""
    with pytest.raises(UnsafeURLError, match="loopback"):
        validate_url("https://feeds.example.com/rss", resolver=resolver_for("127.0.0.1"))


def test_metadata_endpoint_is_rejected() -> None:
    with pytest.raises(UnsafeURLError, match="link-local"):
        validate_url("http://169.254.169.254/latest/meta-data/", resolver=PUBLIC)


def test_every_resolved_address_must_be_safe() -> None:
    """A host mixing a public and a private record is rejected outright."""
    with pytest.raises(UnsafeURLError, match="private"):
        validate_url(
            "https://mixed.example.com/rss",
            resolver=resolver_for("93.184.216.34", "10.0.0.1"),
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://example.com/feed.xml",
        "gopher://example.com/1",
        "data:text/plain,hello",
        "jar:http://example.com/!/",
    ],
)
def test_non_http_schemes_are_rejected(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_url(url, resolver=PUBLIC)


@pytest.mark.parametrize("port", [22, 25, 3306, 5432, 6379, 8080, 11434])
def test_non_web_ports_are_rejected(port: int) -> None:
    """Port 11434 is Ollama's. The guard must not become a local port scanner."""
    with pytest.raises(UnsafeURLError, match="port"):
        validate_url(f"https://example.com:{port}/rss", resolver=PUBLIC)


def test_credentials_in_url_are_rejected() -> None:
    with pytest.raises(UnsafeURLError, match="credentials"):
        validate_url("https://user:pass@example.com/rss", resolver=PUBLIC)


def test_url_without_host_is_rejected() -> None:
    with pytest.raises(UnsafeURLError, match="no host"):
        validate_url("https:///rss", resolver=PUBLIC)


def test_valid_url_returns_the_validated_target() -> None:
    target = validate_url("https://example.com/rss", resolver=PUBLIC)

    assert target.host == "example.com"
    assert target.port == 443
    assert target.scheme == "https"
    assert str(target.connect_address) == "93.184.216.34"


def test_default_ports_are_filled_in() -> None:
    assert validate_url("http://example.com/rss", resolver=PUBLIC).port == 80
    assert validate_url("https://example.com/rss", resolver=PUBLIC).port == 443
