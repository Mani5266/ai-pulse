"""Article normalisation: attach canonical URL, identity and content hash.

Runs immediately after parsing, before anything tries to compare two articles. Every
later stage assumes these fields are populated.
"""

from __future__ import annotations

from collections.abc import Iterable

from app.core.models import Article
from app.ingestion.canonical import canonicalize
from app.ingestion.hashing import article_id, content_hash


def enrich(article: Article) -> Article:
    """Return a copy of ``article`` with ``canonical_url``, ``id`` and ``content_hash``.

    Articles are frozen, so this returns a new record rather than mutating one. Running
    it twice on the same article yields the same values.
    """
    canonical = canonicalize(str(article.url))
    return article.model_copy(
        update={
            "canonical_url": canonical,
            "id": article_id(canonical),
            "content_hash": content_hash(article.title, article.summary),
        }
    )


def enrich_all(articles: Iterable[Article]) -> list[Article]:
    """Enrich a batch, preserving order."""
    return [enrich(article) for article in articles]
