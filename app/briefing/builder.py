"""Building the briefing from analysed events.

One rule governs this stage: **a story without a model-written summary is dropped, not
published.** The alternative would be to fall back to the article's own headline and
summary, which reads fine and is exactly the failure the product exists to avoid — a
briefing that quietly degrades into a feed reader while still looking like a briefing.
Publishing four verified stories is better than publishing five where one is unsupported.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime

from app.briefing.models import Briefing, BriefingStats, Source, Story
from app.core.models import Article
from app.llm.analysis import AnalysedEvent

logger = logging.getLogger(__name__)

MAX_SOURCES_PER_STORY = 4
"""Enough to show corroboration without turning the briefing into a link dump."""


def _sources_for(event_article_ids: Sequence[str], articles: dict[str, Article]) -> list[Source]:
    """Resolve an event's articles into citable sources, one per publisher.

    De-duplicated by source: four TechCrunch links prove nothing that one does, while four
    *different* publishers are the corroboration signal the reader is looking for.
    """
    sources: list[Source] = []
    seen: set[str] = set()

    for article_id in event_article_ids:
        article = articles.get(article_id)
        if article is None or article.source_id in seen:
            continue
        seen.add(article.source_id)
        sources.append(
            Source(source_id=article.source_id, title=article.title, url=str(article.url))
        )
        if len(sources) >= MAX_SOURCES_PER_STORY:
            break

    return sources


def build_briefing(
    analysed: Sequence[AnalysedEvent],
    articles: dict[str, Article],
    *,
    day: date,
    limit: int,
    stats: BriefingStats | None = None,
) -> Briefing:
    """Assemble the day's briefing from the analysed events, best first."""
    stories: list[Story] = []
    dropped = 0

    for item in analysed:
        if len(stories) >= limit:
            break

        analysis = item.analysis
        if analysis is None:
            # No supported summary, so no story. See the module docstring.
            dropped += 1
            continue

        stories.append(
            Story(
                event_id=item.event.id,
                headline=analysis.headline,
                what_happened=analysis.what_happened,
                why_it_matters=analysis.why_it_matters,
                developer_impact=analysis.developer_impact,
                category=item.event.category,
                score=item.final_score,
                confidence=analysis.confidence,
                sources=_sources_for(item.event.article_ids, articles),
                first_seen=item.event.first_seen,
                last_updated=item.event.last_updated,
            )
        )

    if dropped:
        logger.info("dropped %d event(s) with no supported summary", dropped)

    return Briefing(
        day=day,
        generated_at=datetime.now(UTC),
        stories=stories,
        stats=stats or BriefingStats(),
    )
