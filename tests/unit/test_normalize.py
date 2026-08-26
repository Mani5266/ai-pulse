"""Enrichment tests: canonical URL, id and content hash attached to articles."""

from __future__ import annotations

from datetime import UTC, datetime

from app.core.models import Article
from app.ingestion.hashing import article_id
from app.ingestion.normalize import enrich, enrich_all

FETCHED_AT = datetime(2026, 8, 26, 6, 0, tzinfo=UTC)


def raw(url: str, title: str = "Title", summary: str | None = None) -> Article:
    return Article(
        url=url,  # type: ignore[arg-type]
        title=title,
        source_id="example",
        fetched_at=FETCHED_AT,
        summary=summary,
    )


def test_enrich_populates_every_derived_field() -> None:
    article = enrich(raw("https://www.example.com/a/?utm_source=x"))

    assert article.canonical_url == "https://example.com/a"
    assert article.id == article_id("https://example.com/a")
    assert article.content_hash


def test_enrich_leaves_the_fetched_url_intact() -> None:
    """The canonical form is an identity key, never the URL that was retrieved."""
    original = "https://www.example.com/a/?utm_source=x"

    assert str(enrich(raw(original)).url) == original


def test_enrich_is_idempotent() -> None:
    once = enrich(raw("https://example.com/a"))
    twice = enrich(once)

    assert once == twice


def test_url_variants_share_one_id() -> None:
    first = enrich(raw("https://example.com/a"))
    second = enrich(raw("http://www.example.com/a/#top"))

    assert first.id == second.id


def test_enrich_all_preserves_order() -> None:
    articles = enrich_all([raw("https://example.com/1"), raw("https://example.com/2")])

    assert [article.canonical_url for article in articles] == [
        "https://example.com/1",
        "https://example.com/2",
    ]
