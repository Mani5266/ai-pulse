"""URL canonicalisation tests.

Cases are drawn from URL shapes that actually appear in the 22 live feeds: newsletter
tracking parameters, AMP variants, mixed ``www`` usage and trailing slashes.
"""

from __future__ import annotations

import pytest

from app.ingestion.canonical import canonicalize, is_tracking_param

BASE = "https://example.com/blog/model-x"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/blog/model-x",
        "https://example.com/blog/model-x/",
        "https://www.example.com/blog/model-x",
        "http://example.com/blog/model-x",
        "https://EXAMPLE.com/blog/model-x",
        "https://example.com./blog/model-x",
        "https://example.com/blog/model-x#section-2",
        "https://example.com//blog//model-x",
        "https://example.com/blog/model-x/amp/",
        "https://example.com/blog/model-x/index.html",
        "https://example.com:443/blog/model-x",
        "http://example.com:80/blog/model-x",
        "https://example.com/blog/model-x?utm_source=newsletter&utm_medium=email",
        "https://example.com/blog/model-x?fbclid=abc123",
        "https://example.com/blog/model-x?ref=hn",
    ],
)
def test_variants_collapse_to_one_canonical_url(url: str) -> None:
    assert canonicalize(url) == BASE


def test_meaningful_query_parameters_are_kept() -> None:
    assert canonicalize("https://example.com/news?id=123") == "https://example.com/news?id=123"


def test_query_parameter_order_does_not_matter() -> None:
    first = canonicalize("https://example.com/news?b=2&a=1")
    second = canonicalize("https://example.com/news?a=1&b=2")

    assert first == second == "https://example.com/news?a=1&b=2"


def test_tracking_parameters_are_stripped_but_others_survive() -> None:
    url = "https://example.com/news?id=7&utm_source=x&ref=y&page=2"

    assert canonicalize(url) == "https://example.com/news?id=7&page=2"


def test_non_default_port_is_part_of_identity() -> None:
    assert canonicalize("https://example.com:8443/rss") == "https://example.com:8443/rss"


def test_root_path_is_preserved() -> None:
    assert canonicalize("https://example.com") == "https://example.com/"
    assert canonicalize("https://example.com/") == "https://example.com/"


def test_short_hosts_keep_their_www_label() -> None:
    """``www.com`` is a domain in its own right; stripping it would change the host."""
    assert canonicalize("https://www.com/x") == "https://www.com/x"


def test_different_articles_stay_different() -> None:
    assert canonicalize("https://example.com/a") != canonicalize("https://example.com/b")
    assert canonicalize("https://a.com/x") != canonicalize("https://b.com/x")
    assert canonicalize("https://example.com/news?id=1") != canonicalize(
        "https://example.com/news?id=2"
    )


def test_subdomains_are_not_merged() -> None:
    """Only ``www`` is noise. ``blog.`` and ``news.`` are different sites."""
    assert canonicalize("https://blog.example.com/x") != canonicalize("https://example.com/x")


def test_percent_encoding_is_normalised() -> None:
    assert canonicalize("https://example.com/blog/model%2Dx") == BASE


def test_unicode_host_is_idna_encoded() -> None:
    assert canonicalize("https://exämple.com/x") == "https://xn--exmple-cua.com/x"


def test_case_is_preserved_in_the_path() -> None:
    """Hosts are case-insensitive; paths are not."""
    assert canonicalize("https://example.com/Model-X") == "https://example.com/Model-X"


def test_malformed_input_does_not_raise() -> None:
    assert canonicalize("not a url") == "not a url"
    assert canonicalize("") == ""


def test_canonicalisation_is_idempotent() -> None:
    once = canonicalize("https://www.example.com/blog/model-x/?utm_source=x#top")

    assert canonicalize(once) == once


@pytest.mark.parametrize(
    "name", ["utm_source", "UTM_Campaign", "fbclid", "gclid", "ref", "mc_cid", "pk_campaign"]
)
def test_tracking_parameters_are_recognised(name: str) -> None:
    assert is_tracking_param(name) is True


@pytest.mark.parametrize("name", ["id", "page", "q", "year", "v"])
def test_content_parameters_are_not_treated_as_tracking(name: str) -> None:
    assert is_tracking_param(name) is False
