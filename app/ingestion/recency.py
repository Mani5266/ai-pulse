"""Recency filtering.

The stage that separates a news briefing from a digest of the archive.

An RSS feed does not hand you "what is new" — it hands you its current window, and the
size of that window is entirely up to the publisher. TechCrunch's twenty items are one
day; a working engineer's blog with twenty items is a year. Ingesting a feed and treating
every item as news therefore produces exactly what the first run of this pipeline produced:
517 articles spanning thirteen months, and a "daily briefing" led by a release from April.

Filtering by publication date against the run's window is the whole fix, and it belongs
here rather than in the fetcher, because a feed's whole window is still worth fetching:
older items are what let deduplication and clustering recognise a story they have already
seen.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from app.core.models import Article
from app.storage.state import Window

logger = logging.getLogger(__name__)


@dataclass
class RecencyResult:
    """Articles inside the window, and a record of what was left out."""

    fresh: list[Article] = field(default_factory=list)
    stale: list[Article] = field(default_factory=list)

    @property
    def input_count(self) -> int:
        return len(self.fresh) + len(self.stale)

    def stats(self) -> dict[str, int | float]:
        return {
            "input": self.input_count,
            "fresh": len(self.fresh),
            "stale": len(self.stale),
            "fresh_rate": round(len(self.fresh) / self.input_count, 3) if self.input_count else 0.0,
        }

    def oldest_kept(self) -> Article | None:
        dated = [article for article in self.fresh if article.published_at]
        return min(
            dated, key=lambda article: article.published_at or article.fetched_at, default=None
        )


def filter_recent(articles: Sequence[Article], window: Window) -> RecencyResult:
    """Split articles into those the run is responsible for and those it is not.

    An article with no publication date falls back to when it was fetched. That is
    generous — an undated item currently sitting in a feed is usually genuinely current —
    and the alternative, dropping it, would silently lose every item from the feeds that
    omit dates.
    """
    result = RecencyResult()

    for article in articles:
        moment = article.published_at or article.fetched_at
        if window.covers(moment):
            result.fresh.append(article)
        else:
            result.stale.append(article)

    if result.stale:
        logger.info(
            "recency: %d of %d articles are older than the window",
            len(result.stale),
            result.input_count,
        )

    return result
