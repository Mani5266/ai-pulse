"""SSRF guard.

Feed URLs come from a registry file, but redirects come from the open internet, so every
hop is validated before a socket is opened.

The check is performed on the **resolved address**, not on the hostname. A hostname
allowlist or a regular expression on the host is not a defence: an attacker-controlled
domain can resolve to ``127.0.0.1`` or to the cloud metadata address ``169.254.169.254``
just as easily as to a public address.

Residual risk: DNS rebinding. Validation and connection are separate operations, so a
record whose TTL expires in between could resolve differently the second time. The
fetcher closes that gap by connecting to the address this module validated rather than
re-resolving the name. See ``docs/SECURITY.md``.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.core.errors import UnsafeURLError

ALLOWED_SCHEMES: frozenset[str] = frozenset({"http", "https"})
"""Only plain HTTP and HTTPS. No file://, ftp://, gopher:// or data:."""

ALLOWED_PORTS: frozenset[int] = frozenset({80, 443})
"""Feeds live on standard web ports. Anything else is a port scan in disguise."""

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

Resolver = Callable[[str, int], list[IpAddress]]
"""Hostname resolver. Injectable so tests need no DNS."""


@dataclass(frozen=True, slots=True)
class ValidatedTarget:
    """A URL that passed every check, plus the addresses it resolved to."""

    url: str
    scheme: str
    host: str
    port: int
    addresses: tuple[IpAddress, ...]

    @property
    def connect_address(self) -> IpAddress:
        """The address the fetcher should connect to.

        Connecting to a validated address rather than re-resolving the hostname is what
        makes the check meaningful under DNS rebinding.
        """
        return self.addresses[0]


def _unwrap(address: IpAddress) -> IpAddress:
    """Reduce an IPv4-mapped or 6to4 IPv6 address to the IPv4 address it embeds.

    ``::ffff:127.0.0.1`` is loopback, but only after unwrapping.
    """
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address.sixtofour is not None:
            return address.sixtofour
    return address


def rejection_reason(address: IpAddress) -> str | None:
    """Return why this address is not safe to connect to, or None if it is.

    Ordered from most specific to most general so the error message is useful.
    """
    address = _unwrap(address)

    if address.is_unspecified:
        return "unspecified address"
    if address.is_loopback:
        return "loopback address"
    if address.is_link_local:
        # Covers 169.254.169.254, the cloud instance metadata endpoint.
        return "link-local address"
    if address.is_private:
        return "private address"
    if address.is_multicast:
        return "multicast address"
    if address.is_reserved:
        return "reserved address"
    if not address.is_global:
        # Catch-all for ranges the specific checks above do not name.
        return "non-global address"
    return None


def is_safe_address(address: IpAddress) -> bool:
    """True when a connection to this address is permitted."""
    return rejection_reason(address) is None


def _resolve(host: str, port: int) -> list[IpAddress]:
    """Resolve a hostname to every address it offers.

    Every address is validated, not only the one that would be used, so a host that
    mixes public and private records is rejected outright.
    """
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"cannot resolve host {host!r}: {exc}") from exc

    addresses: list[IpAddress] = []
    for info in infos:
        sockaddr = info[4]
        try:
            address = ipaddress.ip_address(sockaddr[0])
        except ValueError:  # pragma: no cover - getaddrinfo returns valid literals
            continue
        if address not in addresses:
            addresses.append(address)

    if not addresses:
        raise UnsafeURLError(f"host {host!r} resolved to no usable address")
    return addresses


def validate_url(url: str, *, resolver: Resolver | None = None) -> ValidatedTarget:
    """Validate a URL and resolve it, or raise :class:`UnsafeURLError`.

    ``resolver`` exists for tests: any callable taking ``(host, port)`` and returning a
    list of addresses. Production code leaves it as None and uses DNS.
    """
    parts = urlsplit(url)

    if parts.scheme not in ALLOWED_SCHEMES:
        raise UnsafeURLError(f"scheme {parts.scheme!r} is not allowed")

    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")

    if parts.username or parts.password:
        # user:pass@host is a classic filter-bypass trick and no feed needs it.
        raise UnsafeURLError("credentials in URL are not allowed")

    try:
        port = parts.port or DEFAULT_PORTS[parts.scheme]
    except ValueError as exc:
        raise UnsafeURLError(f"invalid port in URL: {exc}") from exc

    if port not in ALLOWED_PORTS:
        raise UnsafeURLError(f"port {port} is not allowed")

    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        resolve: Resolver = _resolve if resolver is None else resolver
        addresses = resolve(host, port)
    else:
        # A URL written as an IP literal never reaches DNS, so validate it directly.
        # http://169.254.169.254/ must be rejected on its own terms.
        addresses = [literal]

    for address in addresses:
        reason = rejection_reason(address)
        if reason is not None:
            raise UnsafeURLError(f"{host} resolves to {address} ({reason})")

    return ValidatedTarget(
        url=url,
        scheme=parts.scheme,
        host=host,
        port=port,
        addresses=tuple(addresses),
    )
