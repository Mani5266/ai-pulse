"""Exception hierarchy.

One base class so a caller can catch everything this project raises without also
swallowing unrelated library errors.
"""

from __future__ import annotations


class AIPulseError(Exception):
    """Base class for every error raised by AI-Pulse."""


class ConfigError(AIPulseError):
    """Configuration or source-registry file is missing or malformed."""


class FetchError(AIPulseError):
    """A network fetch failed. Recoverable: the pipeline skips the source."""


class UnsafeURLError(FetchError):
    """A URL was rejected before any connection was attempted.

    Raised by the SSRF guard for disallowed schemes, ports, or addresses that resolve
    into private, loopback, link-local, or otherwise reserved ranges.
    """


class ResponseTooLargeError(FetchError):
    """The response exceeded the configured maximum size while streaming."""


class TooManyRedirectsError(FetchError):
    """The redirect chain exceeded the configured limit."""


class FeedParseError(AIPulseError):
    """A feed body could not be parsed into usable entries."""
