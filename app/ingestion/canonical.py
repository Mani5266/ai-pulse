"""URL canonicalisation.

The same article reaches the pipeline under many URLs::

    https://example.com/blog/model-x
    https://example.com/blog/model-x/
    https://www.example.com/blog/model-x?utm_source=newsletter&utm_medium=email
    http://example.com/blog/model-x#section-2
    https://example.com/blog/model-x/amp/

All five are one article. Canonicalisation reduces them to a single key so that the
cheapest deduplication check — string equality — catches the majority of duplicates
before anything more expensive runs.

Two choices here are deliberate and worth stating:

* **The scheme is forced to https.** A canonical URL is an identity key, not a fetch
  target: an article served over both schemes is one article, not two.
* **A leading ``www.`` is dropped.** Publishers mix the two freely within a single feed.

Neither transformation is ever used to fetch anything. The URL that was actually
retrieved is kept on the article record.
"""

from __future__ import annotations

import contextlib
import re
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

TRACKING_PARAMS: frozenset[str] = frozenset(
    {
        "cmpid",
        "ref",
        "ref_src",
        "referrer",
        "source",
        "fbclid",
        "gclid",
        "dclid",
        "msclkid",
        "twclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "_hsenc",
        "_hsmi",
        "hsctatracking",
        "spm",
        "share",
        "sh",
        "smid",
        "cmp",
        "at_medium",
        "at_campaign",
        "ncid",
        "sr_share",
        "guccounter",
    }
)
"""Parameters that identify how a reader arrived, never which article they arrived at."""

TRACKING_PREFIXES: tuple[str, ...] = ("utm_", "at_custom", "pk_", "piwik_", "mtm_")

DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

_MULTI_SLASH = re.compile(r"/{2,}")
_AMP_SUFFIX = re.compile(r"/amp/?$", re.IGNORECASE)
_INDEX_SUFFIX = re.compile(r"/index\.(html?|php|aspx?)$", re.IGNORECASE)

# Characters that are safe unencoded in a path. Everything else stays percent-encoded.
_PATH_SAFE = "/:@!$&'()*+,;=~-._"


def is_tracking_param(name: str) -> bool:
    """True when a query parameter carries attribution rather than identity."""
    lowered = name.lower()
    return lowered in TRACKING_PARAMS or lowered.startswith(TRACKING_PREFIXES)


def _canonical_host(host: str) -> str:
    """Lower-case, IDNA-encode, and strip a leading ``www.`` and any trailing dot."""
    host = host.strip().rstrip(".").lower()
    # An unencodable label leaves the lower-cased form, which is the best available
    # answer; the caller only ever uses this as a key.
    with contextlib.suppress(UnicodeError, UnicodeDecodeError):
        host = host.encode("idna").decode("ascii")
    if host.startswith("www.") and host.count(".") > 1:
        host = host[4:]
    return host


def _canonical_path(path: str) -> str:
    """Normalise percent-encoding, collapse slashes, and strip AMP and index suffixes."""
    if not path:
        return "/"

    path = quote(unquote(path), safe=_PATH_SAFE)
    path = _MULTI_SLASH.sub("/", path)
    path = _INDEX_SUFFIX.sub("", path)
    path = _AMP_SUFFIX.sub("", path)

    if len(path) > 1:
        path = path.rstrip("/")
    return path or "/"


def _canonical_query(query: str) -> str:
    """Drop tracking parameters and sort the rest so ordering cannot create a variant."""
    if not query:
        return ""
    pairs = [
        (name, value)
        for name, value in parse_qsl(query, keep_blank_values=True)
        if not is_tracking_param(name)
    ]
    if not pairs:
        return ""
    return urlencode(sorted(pairs), doseq=True)


def canonicalize(url: str) -> str:
    """Return the canonical form of ``url``.

    Falls back to the input, stripped, if the URL cannot be parsed into a host — a
    malformed URL must not crash ingestion, and an unparseable string is still a usable
    (if poor) key.
    """
    parts = urlsplit(url.strip())

    if not parts.hostname:
        return url.strip()

    host = _canonical_host(parts.hostname)
    port = parts.port

    # A non-default port is part of the identity; a default one never is, under either
    # scheme, since the canonical form is always https.
    netloc = host
    if (
        port is not None
        and port != DEFAULT_PORTS["https"]
        and port != DEFAULT_PORTS.get(parts.scheme.lower())
    ):
        netloc = f"{host}:{port}"

    return urlunsplit(
        (
            "https",
            netloc,
            _canonical_path(parts.path),
            _canonical_query(parts.query),
            "",
        )
    )
